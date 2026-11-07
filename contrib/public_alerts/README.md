# PublicAlerts

Posts new public alerts for your area to a mesh channel: Alert Ready, earthquake
early warning and Environment Canada, through the NAAD feed.

Install on the bot: `sudo bash contrib/public_alerts/install.sh` (installs turned off).

Settings are on the web viewer's Plugins page. Each Alert channel section has its
own channel, areas and alert level, and an alert goes to every section it matches.
Alert sources are shared by all sections. Sections and sources can be added,
removed and switched on or off there. Changes apply at the next check, but turning
the service on or off needs a bot restart. Channels must exist on the radio.
`#sos` and `#sos-test` are refused.

Area codes are Statistics Canada codes: 7 digits for one area (Salt Spring Island
is 5917027), 4 digits for a regional district. Environment Canada only forecasts by
its own regions, so a Salt Spring section gets every Southern Gulf Islands weather
alert. Province-wide urgent alerts with no location codes go to every section.

Each section also has a lowest warning colour. Environment Canada rates its
warnings yellow, orange or red; set a section to orange to skip the yellow ones.
Emergency alerts always post, and alerts with no colour follow "What to post".

A section can also follow a health authority's drinking water notices. Pick the
authority, list the locations exactly as its page writes them (`Salt Spring Island`),
and tick the notice types. Only Island Health is supported; it has no feed, so its
page is read hourly, and a layout change stops this source (the log says so).
Notices already listed are never posted. First Nations systems aren't on its list.

Test messages are always dropped. The first read of a source posts nothing, so
turning it on doesn't dump the feed. Weather alerts match by region name or a
7-digit code, and other alerts by any code or name.

This is a convenience, not a warning system.

Tests: `python -m pytest --no-cov contrib/public_alerts/test_public_alerts.py`

## Several bots on one channel

The plugin depends on the internet, and a bot inside an affected area can lose
power or its connection when it matters most. Bots elsewhere on the mesh can
cover for it over the air.

Give every bot the same sections and sources, then set two things on each:

- **Other alert bots:** the radio names of the bots, exactly as they appear on
  the mesh. The same list can go on every bot.
- **This bot's role:** Primary, First backup or Second backup. Give each bot a
  different role.

The primary posts first. A backup holds each post and stays quiet if it hears a
named bot post it first. If it hears nothing, it posts. With checks every 60
seconds, the first backup waits 2 minutes and the second 4. The wait is worked
out from **Check every**, so it stays long enough if that changes.

- Weather alerts are matched by title and area. If another bot covered only some
  areas, this bot posts the rest.
- Urgent alerts and water notices are matched by their exact wording, so every
  bot needs the same plugin version and the same areas in that section.
- A lost packet can cause a duplicate. If the mesh splits, each side posts. Both
  are better than a missed alert.
- Only the named bots are listened to, so nobody else can silence an alert by
  typing its wording. Names aren't verified on the mesh, though, so a radio using
  a listed bot's name could.

With **Other alert bots** blank, nothing waits and the bot works as it did before.
