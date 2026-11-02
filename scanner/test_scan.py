"""Tests hors ligne : python3 -m unittest discover -s scanner"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import scan

ZONE = json.loads((scan.CONFIG_PATH).read_text(encoding="utf-8"))["zone"]
STYLES = {s["id"]: s for s in json.loads(scan.STYLES_PATH.read_text(encoding="utf-8"))}
NOW = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
LE_FIL = ("Le Fil", "Saint-Étienne", 45.4436, 4.4005, "v1")


def raw_event(id_, name, date, venue=LE_FIL, genre="Rock", sale=None, presale=None, status="onsale", artist=None):
    classification = {"segment": {"name": "Music"}, "genre": {"name": genre}, "subGenre": {"name": "Undefined"}}
    sales = {"public": {"startDateTime": sale or "1900-01-01T00:00:00Z"}}
    if presale:
        sales["presales"] = [{"name": "Prévente fans", "startDateTime": presale}]
    return {
        "id": id_, "name": name, "url": f"https://www.ticketmaster.fr/{id_}",
        "dates": {"start": {"localDate": date, "localTime": "20:30:00"}, "status": {"code": status}},
        "sales": sales, "classifications": [classification],
        "_embedded": {
            "venues": [{"id": venue[4], "name": venue[0], "city": {"name": venue[1]},
                        "location": {"latitude": str(venue[2]), "longitude": str(venue[3])}}],
            "attractions": [{"name": artist or name}],
        },
    }


def config(**extra):
    return {"zone": ZONE, "artistes": [], "styles": [], "mots_cles": [], "rappels_jours": [7, 1], **extra}


class Geo(unittest.TestCase):
    def inside(self, lat, lon):
        return scan.distance_to_triangle(lat, lon, ZONE["coins"]) <= ZONE["marge_km"]

    def test_geohash_reference(self):
        self.assertEqual(scan.geohash(42.605, -5.603, 5), "ezs42")

    def test_venues_of_the_zone_are_kept(self):
        venues = {"Le Fil": (45.4436, 4.4005), "LDLC Arena, Décines": (45.7245, 4.9819),
                  "Transbordeur": (45.7838, 4.8606), "Zénith d'Auvergne": (45.7347, 3.1996),
                  "Coopérative de Mai": (45.7878, 3.0914), "Vienne": (45.5256, 4.8744)}
        for name, point in venues.items():
            self.assertTrue(self.inside(*point), name)

    def test_far_cities_are_rejected(self):
        for name, point in {"Grenoble": (45.1885, 5.7245), "Valence": (44.9334, 4.8924),
                            "Paris": (48.8566, 2.3522), "Mâcon": (46.3069, 4.8287)}.items():
            self.assertFalse(self.inside(*point), name)

    def test_search_circle_covers_every_corner(self):
        lat, lon, radius = scan.bounding_circle(ZONE["coins"], ZONE["marge_km"])
        for corner in ZONE["coins"]:
            self.assertLess(scan.distance_km((lat, lon), (corner["lat"], corner["lon"])) + ZONE["marge_km"], radius)


class Matching(unittest.TestCase):
    def pick(self, raw, cfg):
        return scan.select_concerts(raw, cfg, STYLES, NOW)[0]

    def test_artist_matches_whole_words_without_accents(self):
        raw = [raw_event("a", "Beyoncé - Tour", "2027-03-01"), raw_event("b", "Museum Night", "2027-03-02")]
        kept = self.pick(raw, config(artistes=["beyonce", "Muse"]))
        self.assertEqual([c["id"] for c in kept], ["a"])

    def test_style_and_exclusions(self):
        raw = [raw_event("a", "Groupe", "2027-03-01", genre="Hip-Hop/Rap"),
               raw_event("b", "Autre", "2027-03-02", genre="Jazz")]
        kept = self.pick(raw, config(styles=["rap"], inclure_sans_style=False))
        self.assertEqual([c["id"] for c in kept], ["a"])

    def test_unclassified_follows_the_setting(self):
        raw = [raw_event("a", "Inconnu", "2027-03-01", genre="Undefined")]
        self.assertEqual(self.pick(raw, config(styles=["rock"], inclure_sans_style=False)), [])
        self.assertEqual(self.pick(raw, config(styles=["rock"]))[0]["raisons"], ["Style non renseigné"])

    def test_outside_zone_and_past_events_are_dropped(self):
        grenoble = ("Summum", "Grenoble", 45.16, 5.74, "v2")
        raw = [raw_event("a", "Loin", "2027-03-01", venue=grenoble), raw_event("b", "Passé", "2026-10-01")]
        self.assertEqual(self.pick(raw, config(styles=["tout"])), [])

    def test_vip_duplicates_are_merged(self):
        raw = [raw_event("b", "Artiste VIP", "2027-03-01", artist="Artiste", sale="2026-12-01T09:00:00Z"),
               raw_event("a", "Artiste", "2027-03-01", artist="Artiste", presale="2026-11-20T09:00:00Z")]
        kept = self.pick(raw, config(styles=["tout"]))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["ids"], ["a", "b"])
        self.assertEqual([s["type"] for s in kept[0]["ventes"]], ["prevente", "publique"])


class Alerts(unittest.TestCase):
    def concerts(self, raw):
        return scan.select_concerts(raw, config(styles=["tout"]), STYLES, NOW)[0]

    def test_first_run_stays_silent_then_new_concert_is_reported_once(self):
        state = {}
        first = self.concerts([raw_event("a", "Ancien", "2027-03-01")])
        self.assertEqual(scan.compare(first, state, [7, 1], NOW, True)["nouveaux"], [])
        second = self.concerts([raw_event("a", "Ancien", "2027-03-01"), raw_event("b", "Nouveau", "2027-04-01")])
        self.assertEqual([c["id"] for c in scan.compare(second, state, [7, 1], NOW, False)["nouveaux"]], ["b"])
        self.assertEqual(scan.compare(second, state, [7, 1], NOW, False)["nouveaux"], [])

    def test_each_reminder_fires_once(self):
        sale = (NOW + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {}
        scan.compare(self.concerts([raw_event("a", "X", "2027-03-01", sale=sale)]), state, [7, 1], NOW, True)
        fired = []
        for days in range(0, 10):
            now = NOW + timedelta(days=days, hours=1)
            concerts = scan.select_concerts([raw_event("a", "X", "2027-03-01", sale=sale)], config(styles=["tout"]), STYLES, now)[0]
            fired.append(len(scan.compare(concerts, state, [7, 1], now, False)["ventes"]))
        self.assertEqual(sum(fired), 2)

    def test_announced_sale_is_reported_and_not_reminded_twice(self):
        state = {}
        scan.compare(self.concerts([raw_event("a", "X", "2027-03-01")]), state, [7, 1], NOW, True)
        sale = (NOW + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        concerts = self.concerts([raw_event("a", "X", "2027-03-01", sale=sale)])
        alerts = scan.compare(concerts, state, [7, 1], NOW, False)
        self.assertEqual((len(alerts["annonces"]), len(alerts["ventes"])), (1, 0))
        self.assertEqual(len(scan.compare(concerts, state, [7, 1], NOW, False)["ventes"]), 0)

    def test_moved_sale_is_announced_again(self):
        state = {}
        first_sale = (NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        moved_sale = (NOW + timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
        scan.compare(self.concerts([raw_event("a", "X", "2027-03-01", sale=first_sale)]), state, [7, 1], NOW, True)
        alerts = scan.compare(self.concerts([raw_event("a", "X", "2027-03-01", sale=moved_sale)]), state, [7, 1], NOW, False)
        self.assertEqual(len(alerts["annonces"]), 1)

    def test_cancellation_and_date_change(self):
        state = {}
        scan.compare(self.concerts([raw_event("a", "X", "2027-03-01"), raw_event("b", "Y", "2027-03-02")]), state, [7, 1], NOW, True)
        alerts = scan.compare(self.concerts([raw_event("a", "X", "2027-03-01", status="canceled"),
                                             raw_event("b", "Y", "2027-05-02")]), state, [7, 1], NOW, False)
        self.assertEqual(sorted(c["type"] for c in alerts["changements"]), ["date", "statut"])

    def test_message_escapes_html_and_splits(self):
        concert = self.concerts([raw_event("a", "Rock & <Roll>", "2027-03-01")])[0]
        text = scan.build_message({"ventes": [], "annonces": [], "changements": [], "nouveaux": [concert]}, [], "", NOW)
        self.assertIn("Rock &amp; &lt;Roll&gt;", text)
        long_text = "\n".join(["ligne " * 20] * 200)
        self.assertTrue(all(len(c) <= scan.TELEGRAM_LIMIT for c in scan.split_message(long_text)))


class Pages(unittest.TestCase):
    def test_hit_is_new_once_and_broken_page_is_reported(self):
        pages = [{"nom": "Salle", "url": "https://a.test/"}, {"nom": "Cassée", "url": "https://b.test/"}]
        cfg = config(artistes=["Shaârghot"], pages=pages)

        def fetch(url):
            if "b.test" in url:
                raise OSError("boom")
            return scan.normalize("Agenda : Shaarghot en concert " + "programme " * 30)

        hits, reports, memory = scan.watch_pages(cfg, {}, fetch)
        self.assertTrue(hits[0]["nouveau"])
        self.assertEqual([r["ok"] for r in reports], [True, False])
        self.assertFalse(scan.watch_pages(cfg, memory, fetch)[0][0]["nouveau"])


class FullRun(unittest.TestCase):
    """Un scan complet avec une fausse API Ticketmaster et un faux Telegram."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "config.json").write_text(json.dumps(config(styles=["rock"], artistes=["Shaârghot"], pages=[])), encoding="utf-8")
        patches = {"CONFIG_PATH": self.dir / "config.json", "CONCERTS_PATH": self.dir / "concerts.json",
                   "STATUS_PATH": self.dir / "status.json", "STATE_PATH": self.dir / "state.json", "REQUEST_PAUSE": 0}
        for name, value in patches.items():
            patcher = mock.patch.object(scan, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict("os.environ", {"TICKETMASTER_API_KEY": "cle-secrete", "TELEGRAM_BOT_TOKEN": "t",
                                             "TELEGRAM_CHAT_ID": "1", "GITHUB_REPOSITORY": "Maxime/concerts"})
        env.start()
        self.addCleanup(env.stop)
        self.sent = []
        self.events = [raw_event("a", "Groupe Rock", "2027-03-01")]

    def fake_api(self, params, key):
        self.assertEqual(params["countryCode"], "FR")
        self.assertEqual(params["locale"], "*")
        return {"_embedded": {"events": self.events}, "page": {"totalElements": len(self.events), "totalPages": 1}}

    def run_scan(self, now=NOW, send=None, others=None):
        send = send or (lambda text, token, chat: self.sent.append(text))
        others = others or (lambda config, state, now_: ([], [], set()))
        with mock.patch.object(scan, "api_get", self.fake_api), mock.patch.object(scan, "send_telegram", send), \
                mock.patch.object(scan, "gather_other_sources", others):
            return scan.run(now)

    def test_two_scans(self):
        self.assertEqual(self.run_scan(), 0)
        self.assertIn("Scanner connecté", self.sent[0])
        self.assertEqual(len(json.loads((self.dir / "concerts.json").read_text())["concerts"]), 1)
        self.events = self.events + [raw_event("b", "Shaârghot", "2027-04-01", genre="Metal")]
        self.run_scan(NOW + timedelta(hours=10))
        self.assertIn("Nouveaux concerts", self.sent[1])
        self.assertIn("Shaârghot", self.sent[1])
        self.run_scan(NOW + timedelta(hours=20))
        self.assertEqual(len(self.sent), 2)

    def test_telegram_failure_keeps_alerts_for_next_scan(self):
        self.run_scan()
        self.events = self.events + [raw_event("b", "Rock Deux", "2027-04-01")]

        def fail(text, token, chat):
            raise scan.ScanError("Telegram a refusé le message")

        self.assertEqual(self.run_scan(NOW + timedelta(hours=10), fail), 1)
        self.run_scan(NOW + timedelta(hours=20))
        self.assertIn("Rock Deux", self.sent[-1])

    def test_api_failure_keeps_results_and_alerts_on_third_time(self):
        self.run_scan()
        before = (self.dir / "concerts.json").read_text()

        def broken(params, key):
            raise scan.ScanError("Ticketmaster a répondu 500.")

        with mock.patch.object(scan, "api_get", broken), \
                mock.patch.object(scan, "gather_other_sources", lambda c, s, n: ([], [], set())), \
                mock.patch.object(scan, "send_telegram", lambda t, a, b: self.sent.append(t)):
            codes = [scan.run(NOW + timedelta(hours=h)) for h in (1, 2, 3, 4)]
        self.assertEqual(codes, [1, 1, 1, 1])
        self.assertEqual((self.dir / "concerts.json").read_text(), before)
        self.assertEqual(sum("échoue" in t for t in self.sent), 1)
        self.assertNotIn("cle-secrete", (self.dir / "status.json").read_text())

    def test_empty_settings_do_not_flood_after_saving(self):
        (self.dir / "config.json").write_text(json.dumps(config(pages=[])), encoding="utf-8")
        self.run_scan()
        self.run_scan(NOW + timedelta(hours=1))
        self.assertEqual(len(self.sent), 1)
        (self.dir / "config.json").write_text(json.dumps(config(styles=["rock"], pages=[])), encoding="utf-8")
        self.run_scan(NOW + timedelta(hours=2))
        self.assertIn("Scanner connecté", self.sent[-1])


def oa_event(uid=1, title="Concert de Nuit Blanche", begin="2027-03-05T20:30:00+0100", keywords=("rock", "concert"),
             location=("Le Fil", "Saint-Étienne", 45.4436, 4.4005), license_id="etalab-2.0", status=1, timings=1,
             description="Soirée live"):
    return {
        "uid": uid, "title": {"fr": title}, "description": {"fr": description}, "keywords": {"fr": list(keywords)},
        "timings": [{"begin": begin, "end": begin}] * timings, "status": status, "attendanceMode": 1,
        "canonicalUrl": f"https://openagenda.com/x/events/{uid}", "updatedAt": "2026-10-12T08:00:00.000Z",
        "location": {"uid": 9, "name": location[0], "adminLevel4": location[1], "latitude": location[2], "longitude": location[3]},
        "rights": {"license": {"id": license_id, "name": "Licence Ouverte 2.0", "url": "https://example.org/lo"},
                   "licensor": {"name": "Ville X", "url": "https://example.org"}},
    }


class OpenAgenda(unittest.TestCase):
    def test_event_is_converted_with_paris_time_and_credit(self):
        concert = scan.parse_openagenda_event(oa_event(), NOW)
        self.assertEqual((concert["date"], concert["heure"]), ("2027-03-05", "20:30"))
        self.assertEqual(concert["styles"], ["rock"])
        self.assertEqual((concert["source"]["nom"], concert["source"]["maj"]), ("Ville X", "2026-10-12"))

    def test_non_concerts_are_ignored(self):
        cases = [oa_event(title="Exposition de photos", keywords=(), description="Une visite"),
                 oa_event(title="Atelier concert pour enfants"), oa_event(title="Marché de Noël", keywords=(), description=""),
                 oa_event(timings=30), oa_event(begin="2020-01-01T20:00:00+0100")]
        self.assertEqual([scan.parse_openagenda_event(c, NOW) for c in cases], [None] * 5)

    def test_statuses_are_mapped(self):
        self.assertEqual(scan.parse_openagenda_event(oa_event(status=6), NOW)["statut"], "canceled")

    def test_only_open_licenses_pass(self):
        allowed = {"etalab-2.0": True, "cc-by-4.0": True, "cc-by-sa-4.0": True, "cc-by-nc-4.0": False, "cc-by-nd-4.0": False, "": False}
        for license_id, expected in allowed.items():
            self.assertEqual(scan.license_is_open({"license": {"id": license_id}}), expected, license_id)

    def test_reading_follows_pages_and_reports(self):
        calls = []
        pages = [{"events": [oa_event(1), oa_event(2, license_id="cc-by-nc-4.0")], "after": ["a", "b"]},
                 {"events": [oa_event(3, title="Autre concert")], "after": None}]

        def fake(path, params, key):
            calls.append((path, dict(params), params))
            return pages[len(calls) - 1]

        state = {"openagenda": {"date": scan.api_date(NOW), "agendas": [{"uid": 42, "titre": "Lyon"}]}}
        with mock.patch.object(scan, "oa_get", fake), mock.patch.object(scan, "REQUEST_PAUSE", 0):
            concerts, report = scan.fetch_openagenda(config(), "k", state, NOW)
        self.assertEqual(sorted(c["id"] for c in concerts), ["oa-1", "oa-3"])
        self.assertIn("1 écarté", report["message"])
        self.assertEqual(calls[0][0], "/agendas/42/events")
        self.assertIn(("after[]", "b"), calls[1][2])
        self.assertLess(calls[0][1]["geo[southWest][lat]"], 45.44)
        self.assertGreater(calls[0][1]["geo[northEast][lng]"], 4.84)

    def test_manual_agendas_skip_discovery(self):
        options = {"agendas": [7]}
        self.assertEqual(scan.choose_agendas(options, "k", {}, NOW), [{"uid": 7, "titre": "7"}])

    def test_discovery_is_cached_for_a_week(self):
        state = {}
        fake = mock.Mock(return_value=[{"uid": 1, "titre": "A"}])
        with mock.patch.object(scan, "discover_agendas", fake):
            scan.choose_agendas({}, "k", state, NOW)
            scan.choose_agendas({}, "k", state, NOW + timedelta(days=6))
            self.assertEqual(fake.call_count, 1)
            scan.choose_agendas({}, "k", state, NOW + timedelta(days=8))
            self.assertEqual(fake.call_count, 2)


class Clermont(unittest.TestCase):
    RECORD = {"id": 123, "nom_libellefr": "Concert de jazz à la Coopé",
              "informationsfeteetmanifestation_categories_item_libellefr": ["Concert"],
              "ouverture_periodesouvertures_datedebut": "2027-02-20", "localisation_commune_nom": "Clermont-Ferrand",
              "geo_point_2d": {"lat": 45.7878, "lon": 3.0914},
              "illustration_url": "https://example.org/photo.jpg", "informations_siteweb": "https://example.org/concert"}

    def test_flat_record(self):
        concert = scan.parse_clermont_record(self.RECORD, "2026-11-02")
        self.assertEqual((concert["id"], concert["date"], concert["ville"]), ("cf-123", "2027-02-20", "Clermont-Ferrand"))
        self.assertEqual((concert["lat"], concert["url"]), (45.7878, "https://example.org/concert"))

    def test_geojson_feature_and_wrappers(self):
        feature = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [3.09, 45.78]},
                   "properties": {k: v for k, v in self.RECORD.items() if k != "geo_point_2d"}}
        concerts, report = scan.fetch_clermont(NOW, lambda: {"features": [feature]})
        self.assertEqual((concerts[0]["lat"], concerts[0]["lon"]), (45.78, 3.09))
        self.assertTrue(report["ok"])

    def test_unknown_format_is_reported_with_its_fields(self):
        concerts, report = scan.fetch_clermont(NOW, lambda: [{"truc": "machin"}])
        self.assertEqual(concerts, [])
        self.assertEqual(report["champs"], ["truc"])

    def test_past_and_non_concert_are_ignored(self):
        self.assertIsNone(scan.parse_clermont_record(self.RECORD, "2028-01-01"))
        other = {**self.RECORD, "nom_libellefr": "Brocante",
                 "informationsfeteetmanifestation_categories_item_libellefr": ["Marché"]}
        self.assertIsNone(scan.parse_clermont_record(other, "2026-11-02"))


class SeveralSources(unittest.TestCase):
    def select(self, raw, extra, **options):
        return scan.select_concerts(raw, config(styles=["tout"], **options), STYLES, NOW, extra)[0]

    def test_same_concert_keeps_ticketmaster_sale_dates(self):
        raw = [raw_event("a", "Skip The Use", "2027-03-05", sale="2026-12-01T09:00:00Z")]
        extra = [scan.parse_openagenda_event(oa_event(title="Concert : Skip The Use"), NOW)]
        kept = self.select(raw, extra)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["source"]["type"], "ticketmaster")
        self.assertEqual(kept[0]["ids"], ["a", "oa-1"])

    def test_different_concerts_stay_separate(self):
        raw = [raw_event("a", "Skip The Use", "2027-03-05")]
        other_day = scan.parse_openagenda_event(oa_event(1, title="Concert : Skip The Use", begin="2027-03-06T20:30:00+0100"), NOW)
        other_town = scan.parse_openagenda_event(oa_event(2, title="Concert : Skip The Use",
                                                          location=("Coopé", "Clermont-Ferrand", 45.7878, 3.0914)), NOW)
        other_band = scan.parse_openagenda_event(oa_event(3, title="Concert de Nuit Blanche"), NOW)
        self.assertEqual(len(self.select(raw, [other_day, other_town, other_band])), 4)

    def test_artist_is_found_in_description_and_text_is_not_published(self):
        extra = [scan.parse_openagenda_event(oa_event(title="Soirée concert", description="Avec Shaârghot en live"), NOW)]
        cfg = config(artistes=["Shaarghot"])
        kept = scan.select_concerts([], cfg, STYLES, NOW, extra)[0]
        self.assertEqual(kept[0]["raisons"], ["Shaarghot"])
        self.assertNotIn("_texte", kept[0])

    def test_concert_found_first_elsewhere_is_not_new_again_on_ticketmaster(self):
        state = {}
        extra = [scan.parse_openagenda_event(oa_event(title="Concert : Skip The Use"), NOW)]
        scan.compare(self.select([], extra), state, [7, 1], NOW, False)
        extra = [scan.parse_openagenda_event(oa_event(title="Concert : Skip The Use"), NOW)]
        both = self.select([raw_event("a", "Skip The Use", "2027-03-05")], extra)
        self.assertEqual(scan.compare(both, state, [7, 1], NOW, False)["nouveaux"], [])

    def test_new_source_is_silent_once_then_alerts(self):
        state = {}
        first = self.select([], [scan.parse_openagenda_event(oa_event(1), NOW)])
        self.assertEqual(scan.compare(first, state, [7, 1], NOW, False, frozenset({"openagenda"}))["nouveaux"], [])
        second = self.select([], [scan.parse_openagenda_event(oa_event(2, title="Autre concert"), NOW)])
        self.assertEqual(len(scan.compare(second, state, [7, 1], NOW, False)["nouveaux"]), 1)

    def test_message_credits_the_source(self):
        concert = self.select([], [scan.parse_openagenda_event(oa_event(), NOW)])[0]
        self.assertIn("Source : Ville X", scan.concert_line(concert))


class FullRunWithSources(FullRun):
    def others(self, failed=False, uid=1):
        def gather(config_, state, now):
            if failed:
                return [], [{"nom": "OpenAgenda", "type": "openagenda", "ok": False, "message": "panne"}], {"openagenda"}
            concert = scan.parse_openagenda_event(oa_event(uid, title=f"Concert rock {uid}"), now)
            return [concert], [{"nom": "OpenAgenda", "type": "openagenda", "ok": True, "message": "ok"}], set()
        return gather

    def test_failed_source_keeps_its_concerts_and_scan_succeeds(self):
        self.run_scan(others=self.others())
        self.assertEqual(self.run_scan(NOW + timedelta(hours=1), others=self.others(failed=True)), 0)
        data = json.loads((self.dir / "concerts.json").read_text())
        self.assertIn("oa-1", [c["id"] for c in data["concerts"]])
        status = json.loads((self.dir / "status.json").read_text())
        self.assertFalse(status["sources"][0]["ok"])

    def test_adding_a_source_to_an_installed_scanner_does_not_flood(self):
        self.run_scan()
        self.sent.clear()
        state = json.loads((self.dir / "state.json").read_text())
        state.pop("sources_vues")
        (self.dir / "state.json").write_text(json.dumps(state))
        self.run_scan(NOW + timedelta(hours=1), others=self.others())
        self.assertEqual(self.sent, [])
        self.run_scan(NOW + timedelta(hours=2), others=self.others(uid=2))
        self.assertIn("Concert rock 2", self.sent[0])


class Paging(unittest.TestCase):
    def test_large_period_is_split(self):
        calls = []

        def fake(params, key):
            calls.append(params)
            span = datetime.fromisoformat(params["endDateTime"][:-1]) - datetime.fromisoformat(params["startDateTime"][:-1])
            total = 1500 if span > timedelta(days=45) else 2
            return {"_embedded": {"events": [{"id": str(len(calls))}]}, "page": {"totalElements": total, "totalPages": 1}}

        with mock.patch.object(scan, "api_get", fake), mock.patch.object(scan, "REQUEST_PAUSE", 0):
            events = scan.search({}, "k", NOW, NOW + timedelta(days=90))
        self.assertEqual(len(events), 2)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
