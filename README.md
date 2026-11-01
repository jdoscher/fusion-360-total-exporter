This is a fork from [Jnessel' github](https://github.com/Jnesselr/fusion-360-total-exporter) with some fixes to make this work in 2026.

# Fusion 360 Total Exporter
Need to export all of your fusion data in a bunch of formats? We've got you covered. This will export your designs across all hubs, projects, files, and components. It exports them in STL, STEP, and IGES format. It also exports the sketches as DXF files. In addition to those, an f3d/f3z file will be exported for each component/assembly file in all of your projects, across all hubs. It doesn't ask what you want written out, it just writes out the latest version of everything, to the folder of your choice.

## How do I use this?
1. Download this project from here, and unzip it (Or clone this repo, if you are familiar with git)
2. Open Fusion 360
3. Click on "Tools" then "Scripts/Add-ins"
4. Click the + button and select the unzipped folder
5. Double click on the "Fusion 360 Total Export" script
6. Acknowledge that this might take a while (There's a menu, but you should probably internalize that)
7. Select where you want the output to go - we suggest making a dedicated folder, to help keep everything nice and organized 
8. Go do something else for a while or enjoy a walk down memory lane as every single design you have is opened, exported, then closed again.

## Why did you make this?
Autodesk just announced that they were limiting features in their free tier to a level that made people a wee bit upset. I pay for Fusion 360, but I get that it's too much of an expense for some people. I had experience with exporting STL files for BotQueue (shhh spoilers) and figured that if I wrote a plugin, no one would have to do manual exports. Yay! Automation reigns supreme!

## What happens if I don't like what this plugin does?
No warranty is implied, etc. etc.

## What if I find a bug?
If an exception occurs, the script will let you know after it has exported everything that it can export. There will be a log file called output.log in your export folder. Submit an issue with that file attached. Please and thank you!

Also, if you can share the file that it failed on, that may help me, but it depends on what the exception actually shows.

## I don't like your style of writing
That wasn't a question. But yeah... me too some days.

## Patched fork: resume and hang fixes

Upstream hangs inside `Documents.open` after many opens ([#45](https://github.com/Jnesselr/fusion-360-total-exporter/issues/45)), and its resume re-opens every design, so a restart hangs at the same spot. This fork:

- skips finished designs before opening them (`.export_done`, or an existing .f3d + .stp);
- marks a design `.export_inprogress` while opening; a design that hung is retried once, then skipped and listed in `skipped_hung.txt`;
- reads an optional `exportignore.txt` in the export folder (one design name or path fragment per line to skip);
- adds `MAX_OPENS_PER_RUN` (0 = unlimited; set e.g. 75 if Fusion still freezes, then relaunch Fusion and re-run) and `MAX_ATTEMPTS` (default 2) at the top of the .py.

Output in the export folder: `output.log` (appended each run), `skipped_hung.txt` (export these by hand), `failed.txt` (delete `.export_failed` in a design's folder to retry).
