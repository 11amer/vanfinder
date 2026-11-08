"""Provenance, seller, and state rules. Synthetic rows only."""
import csv
import tempfile
import unittest
from pathlib import Path

from support import load_build_app

ba = load_build_app()


def write_csv(dirpath, name, rows):
    path = Path(dirpath) / name
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


BD_FIELDS = {
    'Price': '22000', 'Year': '2024', 'Model': 'BrightDrop Zevo 600', 'Miles': '8000',
    'Drivetrain': 'AWD', 'Battery/Range': 'Max Range', 'Battery type': 'Max Range',
    'City/State': 'Dallas, TX', 'Seller': 'Hertz Direct Dealer', 'Phone': '',
    'Listing URL': 'https://www.hertzcarsales.com/v/1', 'VIN': '1GTEST00000000001',
    'Verified': 'Verified on dealer site', 'Verification notes': '', 'Dealer page URL': '',
}


class ProvenanceTests(unittest.TestCase):
    def test_listing_url_is_not_discovery(self):
        row = {
            'Listing URL': 'https://www.cars.com/vehicledetail/abc',
            'Dealer page URL': 'https://www.hertzcarsales.com/dealer',
            'Verification notes': 'seen on CarStory and the dealer site',
        }
        self.assertEqual(ba.discovery_from_row(row), 'Unknown')
        self.assertEqual(ba.listing_site(row['Listing URL']), 'cars.com')
        self.assertNotEqual(ba.listing_site(row['Listing URL']), ba.discovery_from_row(row))

    def test_explicit_columns_only(self):
        self.assertEqual(ba.discovery_from_row({'Source': 'Copart', 'Listing URL': 'https://www.example.com/x'}), 'Copart')
        self.assertEqual(ba.discovery_from_row({'Discovery source': 'Hertz file', 'Source': 'CarStory'}), 'Hertz file')
        self.assertEqual(ba.discovery_from_row({'Original source': 'AutoTempest'}), 'AutoTempest')
        self.assertEqual(ba.discovery_from_row({'Found on': 'Carsforsale'}), 'Carsforsale')
        self.assertEqual(ba.discovery_from_row({'Source': '  '}), 'Unknown')
        self.assertEqual(ba.discovery_from_row({'Discovery source': 'N/A'}), 'Unknown')
        self.assertEqual(ba.discovery_from_row({}), 'Unknown')

    def test_listing_site_host_only(self):
        self.assertEqual(ba.listing_site(''), 'Unknown')
        self.assertEqual(ba.listing_site('https://www.carstory.com/listing/1'), 'carstory.com')
        self.assertEqual(ba.listing_site('www.iaai.com/lot/2'), 'iaai.com')
        self.assertEqual(ba.listing_site('not a url'), 'Unknown')

    def test_state_normalization(self):
        cases = {
            'TX': ('TX', 'Texas'),
            'tx': ('TX', 'Texas'),
            'Tx.': ('TX', 'Texas'),
            'Texas': ('TX', 'Texas'),
            'california': ('CA', 'California'),
            'DC': ('DC', 'District of Columbia'),
            'Washington, DC': ('DC', 'District of Columbia'),
            'D.C.': ('DC', 'District of Columbia'),
            'FL 33101': ('FL', 'Florida'),
            '': ('', 'Unknown'),
            'N/A': ('', 'Unknown'),
            'Unknown': ('', 'Unknown'),
            'Ontario': ('', 'Unknown'),
            'ZZ': ('', 'Unknown'),
        }
        for raw, expected in cases.items():
            self.assertEqual(ba.normalize_state(raw), expected, raw)

    def test_city_state_split(self):
        self.assertEqual(ba.split_city_state('Dallas, TX'), ('Dallas', 'TX'))
        self.assertEqual(ba.split_city_state('Austin, Texas'), ('Austin', 'Texas'))
        self.assertEqual(ba.split_city_state('St. Louis, MO'), ('St. Louis', 'MO'))
        self.assertEqual(ba.split_city_state('Washington, DC'), ('Washington', 'DC'))
        self.assertEqual(ba.split_city_state(''), ('', ''))
        self.assertEqual(ba.split_city_state('TX'), ('', 'TX'))
        # A bare city is not turned into a state.
        self.assertEqual(ba.split_city_state('Ontario'), ('Ontario', ''))

    def test_seller_preserved(self):
        self.assertEqual(ba.normalize_seller('  Hertz Direct Dealer  '), 'Hertz Direct Dealer')
        self.assertEqual(ba.normalize_seller('Fleet Source Co'), 'Fleet Source Co')
        self.assertEqual(ba.normalize_seller(''), 'Unknown')
        self.assertEqual(ba.normalize_seller('   '), 'Unknown')

    def test_same_vin_different_sellers_both_kept(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_csv(d, 'bd.csv', [
                dict(BD_FIELDS),
                dict(BD_FIELDS, Seller='Other Motors', **{
                    'City/State': 'Oakland, CA', 'Price': '27000', 'Drivetrain': 'FWD',
                    'Battery type': 'Standard Range', 'Listing URL': 'https://www.cars.com/vehicledetail/abc',
                    'Verified': 'Checked', 'Verification notes': 'seen on CarStory',
                }),
                dict(BD_FIELDS, VIN='1GTEST00000000002', Seller='Fleet Source Co', **{
                    'City/State': '', 'Price': '', 'Miles': '',
                    'Listing URL': 'https://www.carstory.com/x',
                    'Verified': 'Unconfirmed, possibly sold',
                }),
                dict(BD_FIELDS, VIN='1GTEST00000000003', Seller='Hertz Direct Dealer', **{
                    'City/State': 'Austin, Texas', 'Price': '19000',
                    'Listing URL': 'https://www.carstory.com/y',
                    'Discovery source': 'Hertz Direct Dealer list',
                }),
            ])
            rows = ba.load(str(path), False, 'bd')
        self.assertEqual(len(rows), 4)
        same = [r for r in rows if r['v'] == '1GTEST00000000001']
        self.assertEqual(sorted(r['sel'] for r in same), ['Hertz Direct Dealer', 'Other Motors'])
        other = next(r for r in same if r['sel'] == 'Other Motors')
        self.assertEqual(other['dsc'], 'Unknown')
        self.assertEqual(other['lhs'], 'cars.com')
        self.assertEqual(other['st'], 'CA')
        self.assertEqual(other['p'], 27000)
        fleet = next(r for r in rows if r['sel'] == 'Fleet Source Co')
        self.assertEqual(fleet['st'], '')
        self.assertEqual(fleet['dsc'], 'Unknown')
        self.assertEqual(fleet['lhs'], 'carstory.com')
        self.assertIsNone(fleet['p'])
        self.assertIsNone(fleet['mi'])
        self.assertEqual(fleet['vk'], 'warn')
        found = next(r for r in rows if r['v'] == '1GTEST00000000003')
        self.assertEqual(found['dsc'], 'Hertz Direct Dealer list')
        self.assertEqual(found['lhs'], 'carstory.com')
        self.assertEqual(found['st'], 'TX')
        self.assertEqual(found['stn'], 'Texas')
        self.assertNotEqual(found['dsc'], found['lhs'])

    def test_merge_keeps_other_seller_and_tab_boundary(self):
        primary = [{'v': '1GTEST00000000001', 'sel': 'Hertz Direct Dealer'}]
        incoming = [
            {'v': '1GTEST00000000001', 'sel': 'Hertz Direct Dealer'},
            {'v': '1GTEST00000000001', 'sel': 'Second Lot Seller'},
            {'v': '1GTEST00000000004', 'sel': 'Bay Dealer'},
        ]
        merged = ba.merge_new_sellers(primary, incoming)
        self.assertEqual([r['sel'] for r in merged], ['Second Lot Seller', 'Bay Dealer'])
        kept = ba.drop_tab_vins(primary + merged, {'1GTEST00000000004'})
        self.assertEqual(sorted(r['sel'] for r in kept), ['Hertz Direct Dealer', 'Second Lot Seller'])
        self.assertTrue(all(r['v'] != '1GTEST00000000004' for r in kept))

    def test_template_labels_discovery_separately(self):
        src = Path(ba.__file__).read_text(encoding='utf-8')
        self.assertIn("return r.dsc || 'Unknown'", src)
        self.assertIn('never r.lhs', src)
        self.assertIn('id="ms-sel"', src)
        self.assertIn('id="ms-dsc"', src)
        self.assertIn('id="ms-lhs"', src)
        self.assertIn('id="asum"', src)
        self.assertIn('STATICRYPT_TEMPLATE', src)
        self.assertIn('.site_password', src)
        self.assertIn('<!DOCTYPE html>', src)
        self.assertNotIn('listings_915a.csv', src)


if __name__ == '__main__':
    unittest.main()
