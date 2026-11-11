# hack.build crowd games

Turns the badge into a screen for a room full of people. Phones are the controllers, a host page on the wearer's phone runs the game, and the badge shows it, all over the public relay at relay.clasp.to.

The code lives at [hackbuild/hackbuild-supercon-badge](https://github.com/hackbuild/hackbuild-supercon-badge). Try it at [supercon.hack.build](https://supercon.hack.build), and [how it works](https://supercon.hack.build/how.html) has a diagram.

## What's in it

- Ten crowd modes: a Quiplash style prompt game, name that package (real KiCad footprints and pinouts), live ink, a shared pixel canvas, a 2 fps flip book, a dither photo booth, flash snipe, snake by committee, meme gifs, and a retro arcade where EmulatorJS runs in the host page and phones are the gamepads.
- Four apps in the stock menu: hack.build, Capsule, Sent and Play. They run on this repo's stock firmware and use its panel driver, buttons, LED bar and app framework.
- Remote control signed with HMAC-SHA256 and a one time nonce, screens sent in acked chunks.
- User A and User B run the current game's two main controls. The LED bar counts down each phase.

## Refresh

Quick frames are change-only: the film state goes in plane 0x11 and the new frame in 0x10, and a 10 frame table at 0xB0 drives only pixels that change. That follows the calm frame in [badge os](https://github.com/yigitabi5444/supercon26-badge-os), rewritten in MicroPython. Measured on one badge: 355 to 366 ms a frame, about 2.8 a second, against about 1.25 s for one pass of `lutb_base`. A gif plays a frame every 0.44 s including the file load.

## Install

It needs its own libraries and patches your `main.py`, so it installs from its repo:

```bash
git clone https://github.com/hackbuild/hackbuild-supercon-badge
cd hackbuild-supercon-badge
# create badge/secret.local.json with your wifi and a key, see the README
uv run --no-project --with mpremote --with qrcode --with pillow python tools/deploy.py /dev/cu.usbmodem101
mpremote reset
```

Then open the host page, paste the key, and pick a game.
