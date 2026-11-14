# PocketReader XL

PocketReader XL is an e-reader for the 2026 Billboard Badge. It reads local
PocketReader books and offers a Wi-Fi page for managing books and reader settings.
Reading positions and bookmarks stay on the badge. The application includes
The Time Machine and The War of the Worlds by H. G. Wells.

## Install

These instructions are intended for a badge running the stock firmware. The
installer copies the reader, its display support, fonts, and sample books. It
backs up existing firmware files to a local `backup` directory before replacing
them. Existing sample files and reading data are not overwritten. Install Python
3 and Git first, connect the badge's data port, then run:

```sh
git clone --branch pocketreader-local-release https://github.com/geektoybox/2026-Billboard_Badge.git
cd 2026-Billboard_Badge/user_apps/pocketreader
python3 -m pip install --user mpremote
python3 install.py
```

On Windows, use `py -m pip install --user mpremote` and `py install.py` if
`python3` isn't available. If more than one serial device is connected, supply
`--port` and the badge's serial port to the installer. `python3 install.py
--dry-run` prints the planned operations without changing the badge. The reader
installer and sample books still need an end-to-end test on a fresh badge.

Use the app chooser to start PocketReader XL. Hold User A and User B together to
open Settings, and use Wi-Fi to access the reader's local management page.
In the Wi-Fi menu, **Starter Library** is optional and off by default. Turn it
on and connect to a saved Wi-Fi network to browse the public books alongside
your own. Select a title to download it; turning the option off hides the
available online titles but keeps books already downloaded. No account is required.
In hotspot setup, access is protected by the hotspot password; anyone connected
to that hotspot can open the setup page. When accessed on a saved Wi-Fi network,
the page requires the reader's separate web password. Scan for nearby networks,
choose a result, and enter its password; hidden network names can be typed.
Hold Menu+ and Menu− together for three seconds and release to sleep. Wake with
User B; the reader reopens the book and saved position, or the library page if
no book was open. In Settings, Sleep Timer can be set to five, ten, or thirty
minutes of inactivity, or Never. Automatic sleep waits while Wi-Fi is active.
If power is lost while a book is open, PocketReader also reopens that book and
its saved position when the app is next launched. If the user returned to the
library before powering off, it opens the library instead.

The local management page accepts an optimized sleep-image PNG in the badge's
960 × 672 landscape orientation. It must be at most 256 KB, non-interlaced,
and use only black and white, or black, white, and red. The companion can
resize and convert a picture for the badge. When Quick Boot is enabled, red
pixels are shown as a black-and-white pattern that appears gray; the saved image
is retained, so red returns when Quick Boot is disabled. Reinstalling the
reader preserves an existing sleep image.

## Sample book sources

The public-domain book texts were obtained from Project Gutenberg:
[The Time Machine](https://www.gutenberg.org/ebooks/35) and
[The War of the Worlds](https://www.gutenberg.org/ebooks/36). They have been
repackaged without the Project Gutenberg trademark and license text. These
editions are public domain in the United States; rules elsewhere may differ.
Font license notices are included in the assets folder.
