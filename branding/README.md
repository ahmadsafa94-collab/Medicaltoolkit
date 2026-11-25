# Medic Toolkit logo

- `medic-toolkit-avatar-640.png` -- the bot's Telegram profile picture (BotFather crops it to a circle).
- `medic-toolkit-logo-1024.png` -- the full logo with the name, for posts and the channel.
- `medic-toolkit-icon.svg` -- the icon as a vector, any size. The "AI" lettering is Poppins Bold
  (SIL Open Font License, see webapp/fonts/OFL.txt), embedded in the file.

Colours: sky blue gradient #5DB6F7 -> #1C74D2, navy #0D3B66, white.

## Welcome animation

`welcome.mp4` (640x640, 6 s, loops) is what the bot sends on /start, as a Telegram
animation with the welcome text as its caption. The E3 case opens, and a stack of
books, a capsule and a stethoscope jump out and land in front of it; the lid closes
and "Welcome to MEDIC TOOLKIT" appears. `welcome.gif` is the same animation as a
400px GIF for use outside Telegram.

## Mini App loading screen

`medic-toolkit-loading-icon.svg` is the icon for Telegram's own loading screen
(BotFather: /mybots > the bot > Bot Settings > Configure Mini App > loading
screen). BotFather requires a 512x512 canvas (viewBox 0 0 512 512) with a single
<path>; it is a single-colour silhouette (the cross
and "AI" are cut out), since Telegram draws it as a placeholder shape.
Colours that match the app: background #F2F8FE (light) / #0C1524 (dark),
header #BFE0FB (light) / #12223B (dark).
`medic-toolkit-loading-icon-dark.svg` is the same icon in the dark theme's
lighter blue (#6DB3FF), for BotFather's Dark Mode tab.
