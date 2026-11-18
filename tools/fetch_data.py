"""Collect NBA data for Jump Ball on GitHub's servers and write it next to the site.

Run by .github/workflows/data.yml every 15 minutes. Viewers' browsers then read
these files from the same site as the page, so a network that blocks ESPN, or
an ESPN outage that lasts less than a few hours, doesn't break the calculator.

Usage: python tools/fetch_data.py OUTPUT_DIR
Only the standard library is used. ESPN responses are trimmed to the fields the
page reads, in the same layout ESPN uses, so the page's parsers work on both.
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

ESPN_WEB = os.environ.get('JB_ESPN_WEB', 'https://site.web.api.espn.com')
ESPN_SITE = os.environ.get('JB_ESPN_SITE', 'https://site.api.espn.com')
ESPN_FANTASY = os.environ.get('JB_ESPN_FANTASY', 'https://lm-api-reads.fantasy.espn.com')
PREVIOUS = os.environ.get('JB_PREVIOUS', 'https://thegreatskynet-afk.github.io/Jump-Ball/data/')
WEB = ESPN_WEB + '/apis/common/v3/sports/basketball/nba'
SITE = ESPN_SITE + '/apis/site/v2/sports/basketball/nba'
V2 = ESPN_SITE + '/apis/v2/sports/basketball/nba'
UA = 'Mozilla/5.0 (compatible; JumpBallDataBot/1.0; +https://github.com/thegreatskynet-afk/Jump-Ball)'

NOW = time.time()
today = dt.date.today()
YEAR = today.year + 1 if today.month >= 10 else today.year  # season named by the year it ends, as ESPN does


def get(url, tries=3):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404):
                raise
        except Exception as e:  # network errors, timeouts, bad JSON
            last = e
        time.sleep(1.5 * (k + 1))
    raise last


def pick(d, keys):
    return {k: d[k] for k in keys if isinstance(d, dict) and k in d}


# ---------- trimming: keep only what the page reads ----------
def trim_athlete(a):
    out = pick(a, ['id', 'firstName', 'lastName', 'displayName', 'shortName', 'age', 'teamId', 'teamShortName', 'teamName'])
    if isinstance(a.get('position'), dict):
        out['position'] = pick(a['position'], ['abbreviation'])
    if a.get('teams'):
        out['teams'] = [pick(t, ['id', 'abbreviation', 'displayName', 'name', 'color', 'alternateColor']) for t in a['teams'][:1]]
    return out


def stats_season(year):
    base = WEB + '/statistics/byathlete?region=us&lang=en&contentorigin=espn&isqualified=false&sort=offensive.avgPoints%3Adesc'
    first = get(base + '&page=1&limit=250&season=%d&seasontype=2' % year)
    pages = min(20, int((first.get('pagination') or {}).get('pages') or 1))
    rows = list(first.get('athletes') or [])
    for p in range(2, pages + 1):
        rows += get(base + '&page=%d&limit=250&season=%d&seasontype=2' % (p, year)).get('athletes') or []
    if not rows:
        raise RuntimeError('ESPN returned no players for %d' % year)
    athletes = [{'athlete': trim_athlete(r.get('athlete') or {}),
                 'categories': [pick(c, ['name', 'values', 'totals']) for c in r.get('categories') or []]} for r in rows]
    return {'pagination': {'count': len(athletes), 'limit': len(athletes), 'page': 1, 'pages': 1},
            'categories': [pick(c, ['name', 'names', 'labels']) for c in first.get('categories') or []],
            'requestedSeason': pick(first.get('requestedSeason') or {}, ['year', 'displayName']),
            'athletes': athletes}


def trim_links(links):
    return [pick(l, ['href']) for l in (links or [])[:1]]


def injuries():
    j = get(SITE + '/injuries')
    teams = []
    for t in j.get('injuries') or []:
        recs = []
        for r in t.get('injuries') or []:
            a = r.get('athlete') or {}
            recs.append({**pick(r, ['id', 'status', 'date', 'shortComment', 'longComment', 'type', 'details', 'source']),
                         'athlete': {**pick(a, ['id', 'displayName']), 'links': trim_links(a.get('links')),
                                     'position': pick(a.get('position') or {}, ['abbreviation']),
                                     'team': pick(a.get('team') or {}, ['id', 'abbreviation'])}})
        teams.append({**pick(t, ['id', 'displayName']), 'injuries': recs})
    if not isinstance(j.get('injuries'), list):
        raise RuntimeError('injury feed changed layout')
    return {'timestamp': j.get('timestamp'), 'injuries': teams}


def standings():
    j = get(V2 + '/standings')
    out = []

    def walk(n):
        if isinstance(n, dict):
            if (n.get('standings') or {}).get('entries'):
                out.append({'standings': {'entries': [{'team': pick(e.get('team') or {}, ['id', 'abbreviation']),
                                                       'stats': [pick(s, ['name', 'value']) for s in e.get('stats') or []]}
                                                      for e in n['standings']['entries']]}})
            for c in n.get('children') or []:
                walk(c)
    walk(j)
    if not out:
        raise RuntimeError('standings had no entries')
    return {'children': out}


def trim_articles(arts):
    out = []
    for a in arts or []:
        web = ((a.get('links') or {}).get('web') or {}).get('href')
        out.append({**pick(a, ['headline', 'description', 'published', 'lastModified', 'byline', 'source']),
                    'links': {'web': {'href': web}} if web else {},
                    'categories': [pick(c, ['type', 'athleteId']) for c in a.get('categories') or [] if c.get('type') == 'athlete']})
    return out


def news():
    return {'articles': trim_articles(get(SITE + '/news?limit=50').get('articles'))}


def schedule(team_id):
    j = get(SITE + '/teams/%s/schedule?seasontype=2&season=%d' % (team_id, YEAR))
    ev = []
    for e in j.get('events') or []:
        comps = []
        for c in (e.get('competitions') or [])[:1]:
            st = ((c.get('status') or {}).get('type') or {})
            comps.append({'date': c.get('date'), 'status': {'type': pick(st, ['completed', 'state'])},
                          'competitors': [{**pick(x, ['id', 'homeAway']), 'team': pick(x.get('team') or {}, ['id', 'abbreviation'])}
                                          for x in c.get('competitors') or []]})
        ev.append({**pick(e, ['id', 'date']), 'seasonType': pick(e.get('seasonType') or {}, ['id', 'type']), 'competitions': comps})
    return {'team': pick(j.get('team') or {}, ['id', 'abbreviation']), 'events': ev}


def player(pid):
    out = {}
    try:
        g = get(WEB + '/athletes/%s/gamelog?season=%d' % (pid, YEAR))
        out['gamelog'] = {'labels': g.get('labels'), 'names': g.get('names'),
                          'events': {k: {**pick(v, ['gameDate', 'atVs', 'gameResult', 'score']), 'opponent': pick(v.get('opponent') or {}, ['abbreviation'])}
                                     for k, v in (g.get('events') or {}).items()},
                          'seasonTypes': [{'displayName': t.get('displayName'),
                                           'categories': [{'events': [pick(x, ['eventId', 'stats']) for x in c.get('events') or []]}
                                                          for c in t.get('categories') or []]}
                                          for t in g.get('seasonTypes') or []]}
    except Exception as e:
        out['gamelogError'] = str(e)[:200]
    try:
        o = get(WEB + '/athletes/%s/overview' % pid)
        st = o.get('statistics') or {}
        rw = o.get('rotowire') or {}
        out['overview'] = {'statistics': {'labels': st.get('labels'), 'splits': [pick(s, ['displayName', 'stats']) for s in st.get('splits') or []]},
                           'news': trim_articles((o.get('news') or [])[:8]),
                           'rotowire': pick(rw, ['headline', 'published']) if rw else None}
    except Exception as e:
        out['overviewError'] = str(e)[:200]
    try:
        a = (get(WEB + '/athletes/%s' % pid).get('athlete') or {})
        out['profile'] = {'athlete': {**pick(a, ['id', 'age', 'status']), 'team': pick(a.get('team') or {}, ['id', 'abbreviation']),
                                      'position': pick(a.get('position') or {}, ['abbreviation']),
                                      'injuries': [pick(i, ['status', 'date', 'shortComment']) for i in a.get('injuries') or []]}}
    except Exception as e:
        out['profileError'] = str(e)[:200]
    if 'gamelog' not in out and 'overview' not in out and 'profile' not in out:
        raise RuntimeError(out.get('gamelogError') or 'all player feeds failed')
    return out


def espn_league(lid):
    j = get(ESPN_FANTASY + '/apis/v3/games/fba/seasons/%d/segments/0/leagues/%s?view=mTeam&view=mRoster&view=mSettings' % (YEAR, lid))
    s = j.get('settings') or {}
    sc, rs = s.get('scoringSettings') or {}, s.get('rosterSettings') or {}
    teams = []
    for t in j.get('teams') or []:
        ents = []
        for e in (t.get('roster') or {}).get('entries') or []:
            p = (e.get('playerPoolEntry') or {}).get('player') or {}
            ents.append({**pick(e, ['playerId', 'lineupSlotId']), 'playerPoolEntry': {'player': pick(p, ['id', 'fullName', 'proTeamId', 'defaultPositionId'])}})
        teams.append({**pick(t, ['id', 'name', 'location', 'nickname', 'abbrev', 'owners']),
                      'record': {'overall': pick((t.get('record') or {}).get('overall') or {}, ['wins', 'losses', 'ties'])},
                      'roster': {'entries': ents}})
    return {'id': j.get('id'), 'members': [pick(m, ['id', 'displayName']) for m in j.get('members') or []], 'teams': teams,
            'settings': {'name': s.get('name'), 'size': s.get('size'),
                         'scoringSettings': {'scoringType': sc.get('scoringType'), 'scoringItems': [pick(i, ['statId', 'points', 'isReverseItem']) for i in sc.get('scoringItems') or []]},
                         'rosterSettings': {'lineupSlotCounts': rs.get('lineupSlotCounts')}}}


# ---------- run ----------
def main(out_dir):
    data = os.path.join(out_dir, 'data')
    os.makedirs(os.path.join(data, 'p'), exist_ok=True)
    os.makedirs(os.path.join(data, 'leagues'), exist_ok=True)
    meta = {'at': NOW, 'season': YEAR, 'feeds': {}}

    def write(name, obj):
        with open(os.path.join(data, name), 'w') as f:
            json.dump(obj, f, separators=(',', ':'))

    def previous(name):
        try:
            return get(PREVIOUS + name + '?t=%d' % NOW, tries=1)
        except Exception:
            return None

    prev_meta = previous('meta.json') or {}

    def feed(name, fn, fname):
        try:
            obj = fn()
            write(fname, obj)
            meta['feeds'][name] = {'ok': True, 'at': NOW}
            return obj
        except Exception as e:
            err = '%s: %s' % (type(e).__name__, str(e)[:200])
            old = previous(fname)
            pm = (prev_meta.get('feeds') or {}).get(name) or {}
            if old is not None and pm.get('at'):
                write(fname, old)
                meta['feeds'][name] = {'ok': True, 'at': pm['at'], 'stale': True, 'error': err}
                print('kept previous copy of', name, '-', err)
                return old
            meta['feeds'][name] = {'ok': False, 'error': err}
            print('FAILED', name, '-', err)
            return None

    cur = feed('stats', lambda: stats_season(YEAR), 'stats-%d.json' % YEAR)
    feed('prev', lambda: stats_season(YEAR - 1), 'stats-%d.json' % (YEAR - 1))
    inj = feed('injuries', injuries, 'injuries.json')
    feed('standings', standings, 'standings.json')
    feed('news', news, 'news.json')

    team_ids = sorted({str(a['athlete'].get('teamId')) for a in (cur or {}).get('athletes', []) if a['athlete'].get('teamId')}, key=lambda x: int(x) if x.isdigit() else 0)
    if not team_ids:
        team_ids = [str(i) for i in range(1, 31)]
    def sched_one(t):
        try:
            return t, schedule(t), None
        except Exception as e:
            return t, None, e
    sched = {}
    with cf.ThreadPoolExecutor(6) as ex:
        for tid, obj, err in ex.map(sched_one, team_ids):
            if obj is not None:
                sched[tid] = obj
            else:
                print('schedule failed for team', tid, err)

    def all_schedules():
        if len(sched) < len(team_ids) * 0.8:
            raise RuntimeError('only %d of %d team schedules loaded' % (len(sched), len(team_ids)))
        return sched
    feed('schedule', all_schedules, 'schedules.json')

    ids = set()
    for a in (cur or {}).get('athletes', []):
        cats = {c.get('name'): c for c in a.get('categories') or []}
        gp = ((cats.get('general') or {}).get('values') or [0])[0]
        if a['athlete'].get('id') and (gp or 0) > 0:
            ids.add(str(a['athlete']['id']))
    for t in (inj or {}).get('injuries', []):
        for r in t.get('injuries', []):
            aid = (r.get('athlete') or {}).get('id')
            if not aid:
                href = ' '.join(l.get('href', '') for l in (r.get('athlete') or {}).get('links') or [])
                if '/id/' in href:
                    aid = href.split('/id/')[1].split('/')[0]
            if aid:
                ids.add(str(aid))
    done = failed = 0

    def one(pid):
        try:
            return pid, player(pid), None
        except Exception as e:
            return pid, None, e
    with cf.ThreadPoolExecutor(8) as ex:
        for pid, obj, err in ex.map(one, sorted(ids)):
            if obj is None:
                failed += 1
                old = previous('p/%s.json' % pid)
                if old is not None:
                    write('p/%s.json' % pid, old)
                continue
            obj['at'] = NOW
            write('p/%s.json' % pid, obj)
            done += 1
    meta['feeds']['players'] = {'ok': done > 0, 'at': NOW, 'count': done, 'failed': failed}

    leagues = []
    try:
        with open(os.path.join(os.path.dirname(__file__), 'leagues.json')) as f:
            leagues = json.load(f)
    except Exception:
        pass
    meta['leagues'] = {}
    for lg in leagues:
        if lg.get('platform') != 'espn':
            continue
        key = 'espn-%s' % lg['id']
        try:
            write('leagues/%s.json' % key, espn_league(lg['id']))
            meta['leagues'][key] = {'ok': True, 'at': NOW}
        except Exception as e:
            meta['leagues'][key] = {'ok': False, 'error': str(e)[:200]}
            print('league failed', key, e)

    write('meta.json', meta)
    print(json.dumps({k: v.get('ok') for k, v in meta['feeds'].items()}), 'players', done, 'failed', failed)
    if not (meta['feeds'].get('stats') or {}).get('ok'):
        sys.exit('No player stats at all, not publishing.')


if __name__ == '__main__':
    out = sys.argv[1] if len(sys.argv) > 1 else 'site'
    os.makedirs(out, exist_ok=True)
    for f in ('index.html', 'check.html'):
        if os.path.exists(f):
            shutil.copy(f, os.path.join(out, f))
    main(out)
