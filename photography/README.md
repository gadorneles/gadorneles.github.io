# Photography section

`/photography/` shows totals, a world map of where each photo was taken (shaded by country, and by
state when you click a country), the albums (one per place and year), a timeline of trips, and stats. The photos live in Google Drive. GitHub Actions reads them, makes web-sized
copies, and publishes the site. Nothing photo-related is committed to this repo.

```
Google Drive
  Photography/
    Brasil/                    <- country: English or Portuguese name ("Japão" works), or ISO code
      João Pessoa/             <- place: a city or state; its location goes on the map
        2026/                  <- year: album "João Pessoa", page /photography/joao-pessoa-2026/
          DSCF1234.jpg
          any subfolder/       <- optional; still part of this album
          (titles, descriptions, covers: photography/albums.yaml in this repo)
    South Korea/
      Incheon/
        2026/
    Japan/
      2025/                    <- a year straight under the country also works: album "Japan 2025"
        │
        │  GitHub Actions: when you click "Run workflow" (and on every push)
        │  photography/build.py downloads only new or changed photos
        ▼
  static/photos/*.webp + data/photography.json  ->  hugo  ->  GitHub Pages
```

Albums are listed newest first, each with its dates (for example "Jan 2026").

One trip to several cities: name the place folder after all of them, e.g.
`South Korea/Incheon and Seoul/2026/` ("and", "&", "e", "," all work). Optionally put each day's
photos in a subfolder named after its city (`.../2026/Seoul/`), so photos without GPS land on the
right city instead of the first one in the name.
The place folder name is looked up offline (cities with over 1,000 people, and state names), so
photos without GPS still land on the map at that place. Photos with GPS keep their own spot.

## One-time setup (about 15 minutes)

The site reads Drive with a *service account*: a robot Google account that can only see the
folder you share with it. Use a personal Gmail account for the steps below. University or company
accounts often block service account keys.

1. **Create a Google Cloud project.** Go to <https://console.cloud.google.com/>, click the
   project picker at the top, then **New project** (any name, e.g. `gadorneles-site`).
2. **Enable the Drive API.** In that project: **APIs & Services → Library**, search for
   *Google Drive API*, click **Enable**.
3. **Create the service account.** **IAM & Admin → Service Accounts → Create service account**.
   Name it `site-photos`, click **Done** (it needs no roles).
4. **Download its key.** Open the new service account → **Keys → Add key → Create new key →
   JSON**. A `.json` file downloads. Keep it private; don't commit it.
5. **Share the folder.** In Google Drive, right-click **Photography → Share** and add the
   service account's email (`site-photos@<project>.iam.gserviceaccount.com`, shown in the key
   file as `client_email`) as **Viewer**. Untick "Notify people".
6. **Tell the site which folder.** Open the folder in Drive. The URL ends in
   `/folders/<ID>`. Put that ID in `photography/trips.yaml`:
   ```yaml
   settings:
     drive_folder_id: "1AbCdEf..."
   ```
7. **Map tiles key.** CARTO's map background needs a free key: request one at
   <https://carto.com/basemaps/apikey> (non-commercial use) and put it in `hugo.toml` under
   `[params.photography]` as `cartoKey = "..."`. It is meant to be public, like the key in any
   web map. Without it the map still works but shows an "API key required" notice.
8. **Give GitHub the key.** In the repo: **Settings → Secrets and variables → Actions →
   New repository secret**. Name: `GDRIVE_SERVICE_ACCOUNT_JSON`. Value: paste the *entire*
   contents of the key file.
9. **Commit and push.** Then **Actions → Deploy Hugo site to Pages → Run workflow**.
   The first run downloads everything and takes longer; later runs only fetch new photos.

## Adding photos

1. Upload to `Photography/<Country>/<Place>/<Year>/` in Drive (from your phone or computer).
2. On GitHub: **Actions → Deploy Hugo site to Pages → Run workflow** (works from the GitHub
   mobile app too). The site updates a few minutes later.

Deleting or replacing a photo in Drive removes or updates it on the next run the same way.
Any push to `main` also re-syncs from Drive.

Upload **original files** (Drive keeps the EXIF). That is where the date, GPS, lens and Fujifilm
film simulation come from.

## Titles, descriptions and covers: `photography/albums.yaml`

Each album has a block, keyed by its Drive folder:

```yaml
Brazil/João Pessoa/2026:
  title: João Pessoa
  description: A week in the **Northeast**.   # Markdown allowed
  cover: DSCF0412.JPG                         # file name of the photo on the album card
```

Leave out the year (`Spain/Madrid:`) to match that place in any year. The country can be written in
English or Portuguese. Edit, commit and push; the site rebuilds in a few minutes without
re-processing photos. Empty values
(`""`) mean "use the default". For a new album, copy a block and change the folder. The build log
warns about blocks whose folder doesn't exist (check the spelling of the folder path).

Other keys, for when you need them:

```yaml
  trip: false                        # not a trip: left out of the timeline
  location: João Pessoa              # where photos without GPS go (default: the place folder name)
  hide_on_map: true                  # no photo pins for this album; still counted in the stats
  photos:                            # fixes for single photos, by file name
    DSCF1250.jpg: {location: {lat: 34.9671, lon: 135.7727}, place: Fushimi Inari}
    DSCF1260.jpg: {caption: Rain on Pontocho}
    DSCF1301.jpg: {hidden: true}
```

Places listed under `home_places` in `photography/trips.yaml` (Rio Grande) are never treated as
trips, so they stay out of the timeline whatever the year.

The same keys also work in a `trip.yaml` file inside the album's folder in Drive; `albums.yaml`
wins when both set something.

## Photos without GPS

Each build log (Actions → the run → "Build photos from Google Drive") lists photos that are not on
the map. They still appear in the gallery and still count for the country of their folder. This only
happens when the place folder isn't a known city or state (e.g. "Road trip") or there is none. Add
`location: City` to the album in `albums.yaml`, or give exact coordinates per photo (right-click a spot
in Google Maps to copy them).

Common reasons a photo has no GPS:
- **Fujifilm XApp**: the X-M5 has no GPS. It only writes a location when the phone is connected
  over Bluetooth with location sync on while you shoot.
- **Google Photos**: a location shown there may be *estimated* from your location history and
  may not be in the downloaded file.
- **Uploading from an Android phone**: Android's photo picker removes GPS from photos it hands to
  apps, including the Google Drive app's upload button. Copy the photos to a computer over USB and
  upload them at drive.google.com instead, or turn on "Include location" in the picker if your
  phone shows that option.
- **Sharing**: WhatsApp, and Android's "remove location data" share option, strip GPS.

To check what a photo in Drive really contains:
`python3 photography/build.py --credentials ~/keys/site-photos.json --inspect DSCF1234`
(prints the GPS tags of up to 3 photos whose path contains the text).

## Previewing locally

```bash
sudo apt install libimage-exiftool-perl
pip install -r photography/requirements.txt
python photography/build.py --credentials ~/keys/site-photos.json   # the key file from step 4
npm run dev                                                          # http://localhost:1313/photography/
```

`--source ~/some/folder` builds from a local folder with the same Country/Place/Year layout instead.
`--dry-run` only reports. `--force` re-downloads and re-encodes everything.

## Good to know

- **Cost:** all of this is free for a public repo: GitHub Actions and Pages, the Google Drive
  API and service account (no billing account needed), and CARTO tiles within the free tier.
- **Cache:** processed photos are kept in the Actions cache between runs, but GitHub drops caches
  not used for 7 days. Since runs are occasional, a run will often start from scratch: it
  re-downloads and re-encodes everything from Drive. That takes a few extra minutes and needs no
  action from you.
- **When something breaks:** if Drive can't be read, the run fails and the live site stays as it
  was. GitHub emails you. A single unreadable photo is skipped and logged; it doesn't stop the build.
- **States:** each photo's state comes from its GPS (or the album's place). The UK is split
  into England, Scotland, Wales and Northern Ireland; other countries use their official
  first-level regions (states, provinces, prefectures, districts).
- **Privacy:** published images have all metadata removed. Coordinates are rounded to about 100 m
  (`coord_decimals: 2` in `trips.yaml` gives about 1 km).
- **Size:** 2048 px WebP images take about 300 to 500 KB each. GitHub Pages allows 1 GB per site,
  which is roughly 2,000 to 3,000 photos.

## Files

| Path | Purpose |
|---|---|
| `photography/build.py` | Drive → EXIF, places, resizing, stats → `data/photography.json` |
| `photography/albums.yaml` | album titles, descriptions, covers (edit this one) |
| `photography/trips.yaml` | Drive folder ID, settings, home places |
| `.github/workflows/hugo.yaml` | runs the build on demand and on push; deploys |
| `content/photography/_index.md` | page title and intro text |
| `content/photography/_content.gotmpl` | creates one page per trip from the JSON |
| `layouts/photography/` | map/stats page (`list.html`) and trip page (`single.html`) |
| `assets/js/photography.js`, `assets/css/photography.css` | map, lightbox, styles |
| `photography/states.json` | state/province shapes per country (Natural Earth, public domain, simplified) |
| `static/vendor/` | Leaflet, MarkerCluster, country shapes (world-atlas) |

Map tiles: © OpenStreetMap contributors, © CARTO (free for non-commercial use).
