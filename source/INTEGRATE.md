# Integrate the Van Finder filters

Copy the generator into the local app directory. Do not copy inventory CSVs from anywhere else, do not change `.site_password` or `staticrypt_template.html`, and do not change the daily search scripts.

```bash
cp source/build_app.py /workspace/brightdrop600/build_app.py
cd /workspace/brightdrop600
python3 build_app.py --no-publish
```

Check the local plaintext file, then publish with the usual command (`python3 build_app.py`, no `--no-publish`). Publishing still encrypts with StatiCrypt and the existing password file. This change does not edit the password page.

The public site repo does not store the live CSVs. Leave those files where the daily jobs write them.

## Filters

State, Dealer, Discovered, and Listing site are on the BrightDrop 600, BrightDrop 400 (used and NEW), and Auctions tabs, and on the other tabs this generator already builds. Battery, drivetrain, price, miles, and the rest of the filters still apply and combine with these.

Choosing a state limits the dealer list to sellers who have a listing in that state. Active filters show as chips with a count. Clear all, Reset, and the empty-state button clear them.

Dealer includes every seller string in the file, including Hertz Direct Dealer and fleet sellers.

## Discovery vs listing site

Discovered is filled only from a non-empty `Discovery source`, `Original source`, `Found on`, or `Source` column. If none of those is present, it stays Unknown. The listing URL is not used as the discovery source, including when the notes mention a website.

Listing site is the hostname of `Listing URL` only. It is a separate filter.

To record a real origin later, add a `Discovery source` column. Leave it blank on older rows. Do not backfill it from the current link.

Same VIN at two sellers stays two rows. A VIN that already belongs on the 400 tab is still left off the 600 tab.

## Tests

From this repo:

```bash
python3 -m unittest discover -s source/tests -v
```

The UI test uses Playwright and synthetic rows. It does not read the live CSVs.
