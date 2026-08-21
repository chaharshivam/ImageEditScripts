#!/bin/bash
# Fallback rasterizer (ffmpeg + perl ICO). Canonical generator is render_assets.py.
set -euo pipefail
cd "$(dirname "$0")"
FONT_B="/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT="/System/Library/Fonts/Supplemental/Arial.ttf"

draw_mark() {
  local size="$1" out="$2"
  # scale SVG coords (viewBox 32) to `size`
  # cells at 3,3 / 17.5,3 / 3,17.5 size 11.5; wipe L at BR
  python_scale=  # unused; bash arithmetic
  # Use ffmpeg drawbox. Integer geometry.
  local t=2
  if [ "$size" -ge 64 ]; then t=4; fi
  if [ "$size" -ge 160 ]; then t=10; fi
  if [ "$size" -le 16 ]; then t=1; fi
  local cell=$(( size * 115 / 320 ))
  local gap=$(( size * 30 / 320 ))
  local ox=$(( size * 30 / 320 ))
  local oy=$ox
  local x2=$(( ox + cell + gap ))
  local y2=$(( oy + cell + gap ))
  # wipe L thickness
  local wt=$t
  local x1r=$(( x2 + cell ))
  local y1b=$(( y2 + cell ))

  ffmpeg -y -hide_banner -loglevel error -f lavfi -i "color=c=0xf4f1ea:s=${size}x${size}:d=1" -frames:v 1 \
    -vf "drawbox=x=${ox}:y=${oy}:w=${cell}:h=${cell}:color=0x121410:t=${t}:replace=1,\
drawbox=x=${x2}:y=${oy}:w=${cell}:h=${cell}:color=0x121410:t=${t}:replace=1,\
drawbox=x=${ox}:y=${y2}:w=${cell}:h=${cell}:color=0x121410:t=${t}:replace=1,\
drawbox=x=${x2}:y=$((y1b - wt)):w=${cell}:h=${wt}:color=0xc4f542:t=fill:replace=1,\
drawbox=x=$((x1r - wt)):y=${y2}:w=${wt}:h=${cell}:color=0xc4f542:t=fill:replace=1" \
    "$out"
}

draw_mark 16 favicon-16.png
draw_mark 32 favicon-32.png
draw_mark 64 mark-64.png
draw_mark 180 apple-touch-icon.png

# OG 1200x630
ffmpeg -y -hide_banner -loglevel error -f lavfi -i "color=c=0xf4f1ea:s=1200x630:d=1" -frames:v 1 \
  -vf "drawbox=x=80:y=235:w=56:h=56:color=0x121410:t=8:replace=1,\
drawbox=x=156:y=235:w=56:h=56:color=0x121410:t=8:replace=1,\
drawbox=x=80:y=311:w=56:h=56:color=0x121410:t=8:replace=1,\
drawbox=x=156:y=359:w=56:h=8:color=0xc4f542:t=fill:replace=1,\
drawbox=x=204:y=311:w=8:h=56:color=0xc4f542:t=fill:replace=1,\
drawtext=fontfile=${FONT_B}:text='Framewipe':fontsize=72:fontcolor=0x121410:x=280:y=250,\
drawtext=fontfile=${FONT}:text='Prep frames locally. Nothing uploaded.':fontsize=28:fontcolor=0x5c6156:x=280:y=340" \
  og.png

# PNG-in-ICO (16 + 32)
perl -e '
  sub slurp { local $/; open my $f, "<:raw", $_[0] or die $_[0]; <$f> }
  my $p16 = slurp("favicon-16.png");
  my $p32 = slurp("favicon-32.png");
  my $count = 2;
  my $off = 6 + 16 * $count;
  my $dir = pack("vvv", 0, 1, $count);
  my $e16 = pack("CCCCvvVV", 16, 16, 0, 0, 1, 32, length($p16), $off);
  $off += length($p16);
  my $e32 = pack("CCCCvvVV", 32, 32, 0, 0, 1, 32, length($p32), $off);
  open my $o, ">:raw", "favicon.ico" or die $!;
  print $o $dir, $e16, $e32, $p16, $p32;
  close $o;
'

ls -la favicon-16.png favicon-32.png favicon.ico mark-64.png apple-touch-icon.png og.png
