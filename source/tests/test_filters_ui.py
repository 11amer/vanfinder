"""Browser checks for the filter bar. Synthetic listings only; no live inventory."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from support import load_build_app

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - installed in the test environment
    sync_playwright = None

ba = load_build_app()
SHOTS = Path('/tmp/vf-shots')
VIN_A = '1GTEST00000000001'
VIN_SOLD = '1GTEST00000000002'
VIN_FOUND = '1GTEST00000000003'
VIN_400 = '1GTEST00000000004'
VIN_400B = '1GTEST00000000005'
VIN_NEW = '1GTEST00000000006'
VIN_COP = '1GTEST00000000007'
VIN_UNK = '1GTEST00000000008'


def write_csv(dirpath, name, rows):
    import csv
    path = Path(dirpath) / name
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def bd(**overrides):
    row = {
        'Price': '22000', 'Year': '2024', 'Model': 'BrightDrop Zevo 600', 'Miles': '8000',
        'Drivetrain': 'AWD', 'Battery/Range': 'GM-est ~250 mi (Max Range)', 'Battery type': 'Max Range',
        'City/State': 'Dallas, TX', 'Seller': 'Hertz Direct Dealer', 'Phone': '2145550100',
        'Listing URL': 'https://www.hertzcarsales.com/v/1', 'VIN': VIN_A,
        'Verified': 'Verified on dealer site', 'Verification notes': '', 'Dealer page URL': '',
    }
    row.update(overrides)
    return row


def build_rows(tmp):
    bd_path = write_csv(tmp, 'bd.csv', [
        bd(),
        bd(Seller='Other Motors', **{
            'City/State': 'Oakland, CA', 'Price': '27000', 'Miles': '12000', 'Drivetrain': 'FWD',
            'Battery type': 'Standard Range', 'Battery/Range': 'GM-est ~160 mi (Standard Range)',
            'Listing URL': 'https://www.cars.com/vehicledetail/abc', 'Verified': 'Checked',
            'Verification notes': 'seen on CarStory and the dealer site',
        }),
        bd(VIN=VIN_SOLD, Seller='Fleet Source Co', **{
            'City/State': '', 'Price': '42000', 'Miles': '3000',
            'Listing URL': 'https://www.carstory.com/sold-example',
            'Verified': 'Unconfirmed, possibly sold',
            'Verification notes': 'listing link is CarStory; original source was not recorded',
        }),
        bd(VIN=VIN_FOUND, **{
            'City/State': 'Austin, Texas', 'Price': '19000', 'Miles': '6000',
            'Listing URL': 'https://www.carstory.com/found-example',
            'Discovery source': 'Hertz Direct Dealer list',
        }),
    ])
    close_path = write_csv(tmp, 'close.csv', [
        bd(Seller='Second Lot Seller', **{
            'City/State': 'Dallas, TX', 'Price': '21000', 'Listing URL': 'https://www.example.com/second-lot',
        }),
        bd(VIN=VIN_400, Seller='Should Stay On 400', **{'City/State': 'Houston, TX'}),
    ])
    b4_path = write_csv(tmp, 'b4.csv', [
        bd(VIN=VIN_400, Model='BrightDrop Zevo 400', Seller='Hertz Direct Dealer', **{
            'City/State': 'Houston, TX', 'Price': '23000', 'Listing URL': 'https://www.hertzcarsales.com/v/400',
        }),
        bd(VIN=VIN_400B, Model='BrightDrop Zevo 400', Seller='Bay Dealer', **{
            'City/State': 'Miami, FL', 'Price': '31000', 'Drivetrain': 'FWD', 'Battery type': 'Standard Range',
            'Listing URL': 'https://www.cars.com/v/400',
        }),
    ])
    new_path = write_csv(tmp, 'new.csv', [
        bd(VIN=VIN_NEW, Model='BrightDrop Zevo 400', Seller='New Stock Chevy', **{
            'City/State': 'Nashville, TN', 'Price': '48000', 'Miles': '12',
            'Listing URL': 'https://www.carstory.com/new-example',
        }),
    ])
    au_path = write_csv(tmp, 'au.csv', [
        {
            'VIN': VIN_COP, 'Year': '2023', 'Make': 'BrightDrop', 'Model': 'Zevo 600', 'Price': '5000',
            'Miles': '40000', 'Drivetrain': 'AWD', 'Seller': 'Copart San Diego', 'Seller type': 'auction',
            'City': 'San Diego', 'State': 'CA', 'Listing URL': 'https://www.copart.com/lot/1',
            'Source': 'Copart', 'Battery': 'Max Range',
        },
        {
            'VIN': VIN_UNK, 'Year': '2024', 'Make': 'BrightDrop', 'Model': 'Zevo 400', 'Price': '7000',
            'Miles': '20000', 'Drivetrain': 'AWD', 'Seller': 'Mystery Auction', 'Seller type': 'auction',
            'City': 'Fresno', 'State': '', 'Listing URL': 'https://www.iaai.com/lot/2',
            'Source': '', 'Battery': 'Max Range',
        },
    ])
    rows = ba.load(str(bd_path), False, 'bd')
    close = ba.merge_new_sellers(rows, ba.load(str(close_path), True, 'bd'))
    b4 = ba.load(str(b4_path), False, 'b4')
    fresh = []
    for rec in ba.merge_new_sellers(b4, ba.load(str(new_path), False, 'b4')):
        rec['nu'], rec['cond'] = 1, 'new'
        fresh.append(rec)
    close = ba.drop_tab_vins(close, {r['v'] for r in b4 + fresh})
    auctions = ba.load_extra(str(au_path), 'au', False)
    for rec in auctions:
        rec['bk'] = rec.get('bk') or 'Max Range'
    all_rows = rows + close + b4 + fresh + auctions
    return all_rows


def build_html(rows):
    meta = {
        'updated': 'Nov 8, 2026', 'mtime': '2026-11-08T12:00', 'stamp': 'fixture000001',
        'built': 'Nov 8, 2026 12:00 PM ET',
        'n': sum(1 for r in rows if r['c'] == 'bd' and not r.get('cm')),
        'nc': sum(1 for r in rows if r['c'] == 'bd' and r.get('cm')),
        'min': 19000, 'src': 'fixture.csv', 'srcc': 'fixture-close.csv',
        'cats': {'au': {'n': 2, 'nc': 0, 'ncfp': 0, 'min': 5000, 'updated': 'Nov 8, 2026'},
                 'et': {'n': 0, 'nc': 0, 'ncfp': 0, 'updated': 'Nov 8, 2026'}},
        'b4': {'n': 2, 'nc': 0, 'min': 23000, 'src': 'listings_400.csv', 'srcc': '',
               'nnu': 1, 'minnu': 48000, 'srcnu': 'listings_400_new.csv', 'updated': 'Nov 8, 2026'},
    }
    payload = json.dumps({'meta': meta, 'rows': rows, 'recalls': None}, ensure_ascii=False)
    payload = payload.replace('</', '<\\/')
    return ba.TEMPLATE.replace('/*__DATA__*/', payload)


def showing(page):
    return int(page.locator('#res b').first.inner_text().replace(',', ''))


def option_values(page, key):
    return page.locator(f'#ms-{key} input[type="checkbox"]').evaluate_all('els => els.map(e => e.value)')


def pick(page, key, value):
    page.locator(f'#ms-{key} summary').click()
    page.locator(f'#ms-{key}').locator('input[type="checkbox"]').evaluate_all(
        '(els, value) => { const el = els.find(e => e.value === value); if (!el) throw new Error("missing " + value); el.click(); }',
        value,
    )
    page.keyboard.press('Escape')


@unittest.skipUnless(sync_playwright, 'playwright is not installed')
class FilterUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        rows = build_rows(cls.tmp.name)
        cls.rows = rows
        cls.dup_sellers = [r['sel'] for r in rows if r['v'] == VIN_A]
        html = build_html(rows)
        cls.page_path = Path(cls.tmp.name) / 'fixture.html'
        cls.page_path.write_text(html, encoding='utf-8')
        cls.errors = []
        cls.play = sync_playwright().start()
        cls.browser = cls.play.chromium.launch(headless=True, channel='chrome')
        cls.context = cls.browser.new_context(viewport={'width': 1280, 'height': 1100})
        cls.page = cls.context.new_page()
        cls.page.on('pageerror', lambda err: cls.errors.append(str(err)))
        cls.page.goto(cls.page_path.as_uri())
        cls.page.wait_for_selector('#res')

    @classmethod
    def tearDownClass(cls):
        browser = getattr(cls, 'browser', None)
        if browser:
            browser.close()
        play = getattr(cls, 'play', None)
        if play:
            play.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.errors.clear()
        self.page.set_viewport_size({'width': 1280, 'height': 1100})
        self.page.goto(self.page_path.as_uri())
        self.page.wait_for_selector('#res')
        self.page.evaluate('localStorage.clear(); sessionStorage.clear();')
        self.page.reload()
        self.page.wait_for_selector('#res')

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def shot(self, name, full=True):
        SHOTS.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(SHOTS / name), full_page=full)

    def test_duplicate_vin_sold_flag_and_unknown_discovery(self):
        self.assertEqual(sorted(self.dup_sellers), ['Hertz Direct Dealer', 'Other Motors', 'Second Lot Seller'])
        self.assertFalse(any(r['sel'] == 'Should Stay On 400' for r in self.rows))
        self.assertEqual(showing(self.page), 4)
        self.assertEqual(self.page.locator(f'tr.r[data-v="{VIN_A}"]').count(), 2)
        self.assertEqual(self.page.locator('tr.r[data-sel="Second Lot Seller"]').count(), 0)
        sold = self.page.locator(f'tr.r[data-v="{VIN_SOLD}"]')
        self.assertEqual(sold.get_attribute('data-sold'), '1')
        self.assertEqual(sold.get_attribute('data-dsc'), 'Unknown')
        self.assertEqual(sold.get_attribute('data-lhs'), 'carstory.com')
        self.assertEqual(sold.get_attribute('data-st'), '')
        self.assertIn('Possibly sold', sold.inner_text())
        other = self.page.locator('tr.r[data-sel="Other Motors"]')
        self.assertEqual(other.get_attribute('data-dsc'), 'Unknown')
        self.assertEqual(other.get_attribute('data-lhs'), 'cars.com')
        found = self.page.locator(f'tr.r[data-v="{VIN_FOUND}"]')
        self.assertEqual(found.get_attribute('data-dsc'), 'Hertz Direct Dealer list')
        self.assertEqual(found.get_attribute('data-lhs'), 'carstory.com')
        self.assertNotEqual(found.get_attribute('data-dsc'), found.get_attribute('data-lhs'))

    def test_state_narrows_dealer_and_filters_compose(self):
        pick(self.page, 'st', 'TX')
        self.page.wait_for_function('document.querySelector("#res b").textContent === "2"')
        dealers = option_values(self.page, 'sel')
        self.assertIn('Hertz Direct Dealer', dealers)
        self.assertNotIn('Other Motors', dealers)
        self.assertNotIn('Fleet Source Co', dealers)
        self.page.keyboard.press('Escape')
        self.page.locator('#ms-sel summary').click()
        self.shot('01-state-tx-dealers.png')
        self.page.keyboard.press('Escape')
        pick(self.page, 'sel', 'Hertz Direct Dealer')
        self.page.wait_for_function('document.querySelector("#res b").textContent === "2"')
        self.assertIn('State: TX · Texas', self.page.locator('#asum').inner_text())
        self.assertIn('Dealer: Hertz Direct Dealer', self.page.locator('#asum').inner_text())
        self.assertIn('2 active', self.page.locator('#asum').inner_text())
        pick(self.page, 'bk', 'Max Range')
        self.page.wait_for_function('document.querySelector("#res b").textContent === "2"')
        self.assertIn('AWD', option_values(self.page, 'dt'))
        self.assertNotIn('FWD', option_values(self.page, 'dt'))
        self.page.locator('#maxp').select_option('5000')
        self.page.wait_for_selector('.empty')
        self.assertIn('No vans match', self.page.locator('.empty').inner_text())
        self.shot('02-empty-intersection.png')
        self.page.locator('.empty [data-act="reset"]').click()
        self.page.wait_for_function('document.querySelector("#res b").textContent === "4"')
        self.assertEqual(self.page.locator('#asum').inner_text(), '')
        self.assertEqual(self.page.locator('#ms-st .msv').inner_text(), 'Any')

    def test_discovery_listing_site_and_close_match_seller(self):
        pick(self.page, 'dsc', 'Hertz Direct Dealer list')
        self.page.wait_for_function('document.querySelector("#res b").textContent === "1"')
        only = self.page.locator('tr.r')
        self.assertEqual(only.count(), 1)
        self.assertEqual(only.get_attribute('data-lhs'), 'carstory.com')
        self.assertEqual(only.get_attribute('data-dsc'), 'Hertz Direct Dealer list')
        self.page.locator('#reset').click()
        pick(self.page, 'lhs', 'cars.com')
        self.page.wait_for_function('document.querySelector("#res b").textContent === "1"')
        row = self.page.locator('tr.r')
        self.assertEqual(row.get_attribute('data-sel'), 'Other Motors')
        self.assertEqual(row.get_attribute('data-dsc'), 'Unknown')
        self.page.locator('#reset').click()
        pick(self.page, 'st', 'Unknown')
        self.page.wait_for_function('document.querySelector("#res b").textContent === "1"')
        self.assertEqual(self.page.locator('tr.r').get_attribute('data-sel'), 'Fleet Source Co')
        self.page.keyboard.press('Escape')
        self.page.locator('.achip', has_text='State:').click()
        self.page.wait_for_function('document.querySelector("#res b").textContent === "4"')
        self.page.locator('#close').check()
        self.page.wait_for_selector('tr.r[data-sel="Second Lot Seller"]')
        self.assertEqual(self.page.locator(f'tr.r[data-v="{VIN_A}"]').count(), 3)

    def test_tabs_400_new_and_auctions(self):
        pick(self.page, 'st', 'TX')
        self.page.locator('[data-cat="b4"]').click()
        self.page.wait_for_function('document.querySelector("#res b").textContent === "2"')
        self.assertEqual(self.page.locator('#ms-st .msv').inner_text(), 'Any')
        self.assertIn('NEW (unused) hidden', self.page.locator('#res').inner_text())
        pick(self.page, 'st', 'TX')
        dealers = option_values(self.page, 'sel')
        self.assertEqual(dealers, ['Hertz Direct Dealer'])
        self.page.locator('#reset').click()
        pick(self.page, 'sel', 'New Stock Chevy')
        self.page.wait_for_selector('.empty')
        self.page.locator('#res [data-nu]').click()
        self.page.wait_for_selector(f'tr.r[data-v="{VIN_NEW}"]')
        fresh = self.page.locator(f'tr.r[data-v="{VIN_NEW}"]')
        self.assertEqual(fresh.get_attribute('data-nu'), '1')
        self.assertEqual(fresh.get_attribute('data-st'), 'TN')
        self.page.locator('[data-cat="au"]').click()
        self.page.wait_for_function('document.querySelector("#res b").textContent === "2"')
        cop = self.page.locator(f'tr.r[data-v="{VIN_COP}"]')
        unk = self.page.locator(f'tr.r[data-v="{VIN_UNK}"]')
        self.assertEqual(cop.get_attribute('data-dsc'), 'Copart')
        self.assertEqual(cop.get_attribute('data-lhs'), 'copart.com')
        self.assertEqual(unk.get_attribute('data-dsc'), 'Unknown')
        self.assertEqual(unk.get_attribute('data-lhs'), 'iaai.com')
        self.assertNotEqual(unk.get_attribute('data-dsc'), 'iaai.com')
        pick(self.page, 'dsc', 'Unknown')
        self.page.wait_for_function('document.querySelector("#res b").textContent === "1"')
        self.assertEqual(self.page.locator('tr.r').get_attribute('data-v'), VIN_UNK)
        self.shot('03-auction-unknown-discovery.png')
        self.page.locator('[data-cat="bd"]').click()
        self.page.wait_for_function('document.querySelector("#res b").textContent === "2"')
        self.assertIn('TX', self.page.locator('#ms-st .msv').inner_text())

    def test_keyboard_mobile_and_price_chip(self):
        self.page.locator('#chips [data-val="30000"]').click()
        self.page.wait_for_function('document.querySelector("#res b").textContent === "3"')
        self.assertEqual(self.page.locator(f'tr.r[data-v="{VIN_SOLD}"]').count(), 0)
        self.page.locator('#ms-st summary').focus()
        self.page.keyboard.press('Enter')
        self.page.wait_for_selector('#ms-st input[value="TX"]')
        self.page.locator('#ms-st input[value="TX"]').focus()
        self.page.keyboard.press(' ')
        self.page.wait_for_function('document.querySelector("#res b").textContent === "2"')
        self.page.keyboard.press('Escape')
        self.assertFalse(self.page.locator('#ms-st').evaluate('el => el.open'))
        self.page.locator('#reset').click()
        self.page.wait_for_function('document.querySelector("#res b").textContent === "4"')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.evaluate('localStorage.clear(); sessionStorage.clear();')
        self.page.reload()
        self.page.wait_for_selector('#facets')
        display = self.page.locator('#filters').evaluate('el => getComputedStyle(el).display')
        self.assertEqual(display, 'none')
        box = self.page.locator('#facets').bounding_box()
        self.assertGreater(box['height'], 40)
        self.assertGreater(box['width'], 300)
        self.shot('04-mobile-facets.png', full=False)


if __name__ == '__main__':
    unittest.main()
