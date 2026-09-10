#!/usr/bin/env python3
"""Minimal set: poster PNG, and a GIF that fills left-to-right then rests on the credits card."""
import os
from PIL import Image
import render_min
W=render_min.W

poster=render_min.build(2); poster.save('run4-min.png')
print('run4-min.png',poster.size,f"{os.path.getsize('run4-min.png')/1024:.0f} KB")

NF,HOLD = 96,22                  # fill, then rest on the finished run
frames=[render_min.build(0.8, (i/(NF-HOLD))*W if i < NF-HOLD else None) for i in range(NF)]
pal=[f.convert('P',palette=Image.ADAPTIVE,colors=48) for f in frames]
durs=[95]*NF
durs[-1]=1400
pal[0].save('run4-min.gif',save_all=True,append_images=pal[1:],duration=durs,loop=0,
            optimize=True,disposal=2)
print('run4-min.gif',pal[0].size,f"{len(pal)} frames, {os.path.getsize('run4-min.gif')/1024/1024:.2f} MB")
