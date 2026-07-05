#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "trimesh", "scipy"]
# ///
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Charles Bine
"""Self-contained three.js web viewer for any set of mesh files.

Loads STL/OBJ/GLB/3MF parts,
colors them, embeds a GLB in a single HTML file with orbit controls, a mm grid,
labeled axes, per-part legend, explode toggle, auto-rotate toggle, X/Y/Z clip-plane
sliders, and --group so chosen parts explode as one rigid body (e.g. keep the PCB +
glass + connectors together while the case shells fly off).

Usage:
  uv run viewer.py body.stl lid.stl -o preview.html --title "Pill organizer"
  uv run viewer.py case_body.stl case_panel.stl pcb.stl glass.stl io.stl \\
      --group module=pcb,glass,io -o preview.html   # module stays rigid on explode

Regenerate any time; keep the HTML open in a browser and refresh.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import trimesh

PALETTE = [
    [90, 196, 184, 255], [224, 158, 88, 255], [168, 150, 222, 255],
    [120, 205, 150, 255], [233, 110, 98, 255], [238, 232, 205, 255],
    [110, 160, 235, 255], [222, 140, 190, 255],
]
HEX = ["#5ac4b8", "#e09e58", "#a896de", "#78cd96", "#e96e62", "#eee8cd", "#6ea0eb", "#de8cbe"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+")
    ap.add_argument("-o", "--out", default="preview.html")
    ap.add_argument("--title", default=None)
    ap.add_argument("--group", action="append", default=[], metavar="NAME=stemA,stemB",
                    help="group parts by file stem so they explode as one rigid body (repeatable)")
    args = ap.parse_args()

    meshes, legend = [], []
    for i, f in enumerate(args.files):
        m = trimesh.load(f, force="mesh", process=False)
        m.visual.face_colors = PALETTE[i % len(PALETTE)]
        meshes.append(m)
        legend.append({"name": Path(f).stem, "color": HEX[i % len(HEX)]})

    # --group NAME=stemA,stemB  → parts in a group explode as one rigid unit
    stem_group = {}
    for spec in args.group:
        if "=" not in spec:
            ap.error(f"--group needs NAME=stem,stem (got {spec!r})")
        gname, members = spec.split("=", 1)
        for s in members.split(","):
            if s.strip():
                stem_group[s.strip()] = gname.strip()
    gid_of, groups = {}, []
    for f in args.files:
        stem = Path(f).stem
        key = stem_group.get(stem, "\0" + stem)          # ungrouped → unique singleton
        groups.append(gid_of.setdefault(key, len(gid_of)))
    for lg in legend:
        if lg["name"] in stem_group:
            lg["group"] = stem_group[lg["name"]]

    glb = trimesh.Scene(meshes).export(file_type="glb")
    b64 = base64.b64encode(glb).decode()
    title = args.title or " + ".join(l["name"] for l in legend)

    html = HTML.replace("__GLB__", b64) \
               .replace("__TITLE__", title) \
               .replace("__LEGEND__", json.dumps(legend)) \
               .replace("__GROUPS__", json.dumps(groups))
    out = Path(args.out)
    out.write_text(html)
    print(f"GLB {len(glb)//1024} KB -> {out}  ({out.stat().st_size//1024} KB self-contained)")


HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  html,body { margin:0; height:100%; background:#1b1d22; overflow:hidden;
    font-family:system-ui,sans-serif; color:#d6dae0; }
  canvas { display:block; }
  .tag { position:fixed; top:14px; left:16px; font-size:13px; opacity:.7; }
  .legend { position:fixed; bottom:16px; left:16px; font-size:13px; }
  .sw { display:inline-block; width:11px; height:11px; border-radius:2px; margin:0 5px -1px 14px; }
  #panel { position:fixed; right:14px; top:12px; width:230px; background:#22252c;
    border:1px solid #3a404a; border-radius:9px; padding:10px 12px; font-size:13px; }
  #panel button { width:100%; margin:3px 0; padding:7px 0; font-size:13px;
    background:#2a2e36; color:#e6eaf0; border:1px solid #3a404a; border-radius:7px; cursor:pointer; }
  #panel button:hover { background:#343a44; }
  .cliprow { margin-top:8px; }
  .cliprow label { display:flex; align-items:center; gap:6px; }
  .dslider { position:relative; height:20px; }
  .dslider input[type=range] { position:absolute; left:0; top:0; width:100%; margin:0;
    background:none; pointer-events:none; -webkit-appearance:none; appearance:none; height:20px; }
  .dslider input[type=range]::-webkit-slider-runnable-track { height:4px; background:#3a404a;
    border-radius:2px; margin-top:8px; }
  .dslider input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; appearance:none;
    width:14px; height:14px; border-radius:50%; background:#7fb4e8; margin-top:-5px;
    pointer-events:auto; cursor:pointer; border:none; }
  .dslider input[type=range]::-moz-range-track { height:4px; background:#3a404a; border-radius:2px; }
  .dslider input[type=range]::-moz-range-thumb { width:14px; height:14px; border-radius:50%;
    background:#7fb4e8; pointer-events:auto; cursor:pointer; border:none; }
</style>
<script type="importmap">{ "imports": {
  "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
  "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
}}</script></head><body>
<div class="tag">__TITLE__ &middot; grid = 1&thinsp;cm &middot; drag to orbit &middot; scroll to zoom</div>
<div class="legend" id="legend"></div>
<div id="panel">
  <button id="spin">stop rotation</button>
  <button id="explode">explode</button>
  <div class="cliprow" id="clips"><b>clip planes</b></div>
</div>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const B64 = "__GLB__";
const LEGEND = __LEGEND__;
const GROUPS = __GROUPS__;
function toBuf(b64){ const s=atob(b64), n=s.length, a=new Uint8Array(n);
  for(let i=0;i<n;i++)a[i]=s.charCodeAt(i); return a.buffer; }

document.getElementById('legend').innerHTML = LEGEND.map(l =>
  `<span class="sw" style="background:${l.color}"></span>${l.name}` +
  (l.group ? ` <span style="opacity:.45">&#9656;${l.group}</span>` : '')).join('');

const renderer = new THREE.WebGLRenderer({ antialias:true });
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.1;
renderer.localClippingEnabled = true;
document.body.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x1b1d22);
const camera = new THREE.PerspectiveCamera(38, innerWidth/innerHeight, 0.5, 8000);
camera.up.set(0, 0, 1);                                  // Z up, millimeters

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
const PREFKEY = 'vesper-viewer:' + document.title;
let prefs = {};
try { prefs = JSON.parse(localStorage.getItem(PREFKEY) || '{}'); } catch(e){}
const savePrefs = () => { try { localStorage.setItem(PREFKEY, JSON.stringify(prefs)); } catch(e){} };
controls.autoRotate = prefs.autoRotate === true;   // default OFF; only on if explicitly enabled
controls.autoRotateSpeed = 1.3;

scene.add(new THREE.AmbientLight(0x8893a0, 0.65));
const key = new THREE.DirectionalLight(0xffffff, 2.5);
key.castShadow = true;
key.shadow.mapSize.set(2048, 2048);
key.shadow.bias = -0.0004;
scene.add(key, key.target);
const fill = new THREE.DirectionalLight(0xffffff, 0.4);
scene.add(fill);

function label(text, color, mm){
  const cv = document.createElement('canvas'); cv.width = cv.height = 128;
  const c = cv.getContext('2d');
  c.fillStyle = color;
  c.font = 'bold ' + Math.min(78, Math.floor(190/Math.max(text.length, 1))) + 'px system-ui';
  c.textAlign = 'center'; c.textBaseline = 'middle';
  c.fillText(text, 64, 68);
  const tex = new THREE.CanvasTexture(cv); tex.anisotropy = 4;
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map:tex, transparent:true, depthTest:false }));
  s.scale.set(mm, mm, 1);
  return s;
}
function lineSeg(pts, color, op){
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
  return new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color, transparent:true, opacity:op }));
}

// clip planes: two per axis = a keep-window between the lo and hi handles
const AXES = ['X','Y','Z'];
const EVEC = [new THREE.Vector3(1,0,0), new THREE.Vector3(0,1,0), new THREE.Vector3(0,0,1)];
const clipPlanes = [];
for (const e of EVEC){
  clipPlanes.push(new THREE.Plane(e.clone(), 1e6));            // keep p > lo
  clipPlanes.push(new THREE.Plane(e.clone().negate(), 1e6));   // keep p < hi
}
const clipState = AXES.map(() => ({ on:false, lo:0, hi:0 }));

const pieces = [];
let explodeAmt = 0, explodeTarget = 0;

new GLTFLoader().parse(toBuf(B64), '', (gltf) => {
  const model = gltf.scene;
  const box = new THREE.Box3().setFromObject(model);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const edgeMat = new THREE.LineBasicMaterial({ color: 0x11151c, transparent: true, opacity: 0.5 });

  model.traverse(o => { if (o.isMesh){
    o.castShadow = true; o.receiveShadow = true;
    o.material.roughness = 0.6; o.material.metalness = 0.0;
    o.material.flatShading = true;
    o.material.side = THREE.DoubleSide;
    o.material.clippingPlanes = clipPlanes;
    o.material.clipShadows = true;
    o.material.needsUpdate = true;
    const el = new THREE.LineSegments(new THREE.EdgesGeometry(o.geometry, 18), edgeMat.clone());
    el.material.clippingPlanes = clipPlanes;
    o.add(el);
    const bb = new THREE.Box3().setFromBufferAttribute(o.geometry.attributes.position);
    o.userData = { c: bb.getCenter(new THREE.Vector3()), sz: bb.getSize(new THREE.Vector3()),
                   base: o.position.clone(), gid: GROUPS[pieces.length] };
    pieces.push(o);
  }});
  scene.add(model);

  // explode: normalize each part's offset from the assembly center by the
  // assembly half-size, so concentric stacks (body+lid) separate along the
  // axis where their centers actually differ instead of all lifting together
  const half = size.clone().multiplyScalar(0.5).max(new THREE.Vector3(1,1,1));
  const dist = Math.max(size.x, size.y, size.z) * 0.55;
  // Explode by GROUP, stacked along Z so groups (rigid bodies) clear each other even
  // when one shell encloses another (a case around its module). Order by Z-centroid;
  // space by each group's own Z-extent so nothing overlaps mid-explode.
  const gids = [...new Set(pieces.map(o => o.userData.gid))];
  const info = {};
  for (const g of gids){
    const mem = pieces.filter(o => o.userData.gid === g);
    const avg = k => mem.reduce((a,o)=>a+o.userData.c[k],0)/mem.length;
    const zmin = Math.min(...mem.map(o=>o.userData.c.z - o.userData.sz.z/2));
    const zmax = Math.max(...mem.map(o=>o.userData.c.z + o.userData.sz.z/2));
    info[g] = { cx:avg('x'), cy:avg('y'), cz:avg('z'), span:zmax-zmin };
  }
  const order = gids.slice().sort((a,b)=>info[a].cz - info[b].cz);
  const gap = Math.max(size.x, size.y, size.z) * 0.10;
  const zpos = {}; let acc = 0;
  order.forEach((g,i)=>{ if(i>0) acc += info[order[i-1]].span/2 + gap + info[g].span/2; zpos[g]=acc; });
  const mid = acc / 2;
  for (const o of pieces){
    const gi = info[o.userData.gid];
    o.userData.off = new THREE.Vector3(
      (gi.cx-center.x)/half.x * dist*0.35,           // small lateral nudge
      (gi.cy-center.y)/half.y * dist*0.35,
      (center.z + zpos[o.userData.gid] - mid) - gi.cz);  // layered along Z, group stays rigid
  }
  const minOffZ = Math.min(...pieces.map(o=>o.userData.off.z));
  for (const o of pieces) o.userData.off.z -= minOffZ;   // stay above the pad

  // grid pad: minor 10mm / major 100mm (or 5/25mm for small models), corner at origin-ish
  const small = Math.max(size.x, size.y) < 120;
  const step = small ? 5 : 10, majorEvery = small ? 25 : 100;
  const gx0 = Math.floor(box.min.x/step)*step - 2*step, gx1 = Math.ceil(box.max.x/step)*step + 2*step;
  const gy0 = Math.floor(box.min.y/step)*step - 2*step, gy1 = Math.ceil(box.max.y/step)*step + 2*step;
  const z0 = box.min.z - 0.05;
  const pad = new THREE.Mesh(new THREE.PlaneGeometry(gx1-gx0, gy1-gy0),
    new THREE.ShadowMaterial({ opacity: 0.32 }));
  pad.position.set((gx0+gx1)/2, (gy0+gy1)/2, z0); pad.receiveShadow = true; scene.add(pad);
  const minor = [], major = [];
  for (let x = gx0; x <= gx1 + 0.1; x += step){ (x % majorEvery ? minor : major).push(x,gy0,z0, x,gy1,z0); }
  for (let y = gy0; y <= gy1 + 0.1; y += step){ (y % majorEvery ? minor : major).push(gx0,y,z0, gx1,y,z0); }
  scene.add(lineSeg(minor, 0x3a4250, 0.55));
  scene.add(lineSeg(major, 0x6b7686, 0.9));
  const tag = document.querySelector('.tag');
  tag.innerHTML = tag.innerHTML.replace('1&thinsp;cm', small ? '5&thinsp;mm' : '1&thinsp;cm');

  // axes + a size caption
  const ax0 = new THREE.Vector3(gx0, gy0, z0);
  scene.add(lineSeg([ax0.x,ax0.y,ax0.z, gx1+8,ax0.y,ax0.z], 0xff6b6b, 1));
  scene.add(lineSeg([ax0.x,ax0.y,ax0.z, ax0.x,gy1+8,ax0.z], 0x7bd88f, 1));
  scene.add(lineSeg([ax0.x,ax0.y,ax0.z, ax0.x,ax0.y,box.max.z+12], 0x6ba8ff, 1));
  const lx = label('X', '#ff8f8f', step*2.2); lx.position.set(gx1+14, ax0.y, z0+1); scene.add(lx);
  const ly = label('Y', '#9be3ad', step*2.2); ly.position.set(ax0.x, gy1+14, z0+1); scene.add(ly);
  const lz = label('Z', '#9bc0ff', step*2.2); lz.position.set(ax0.x, ax0.y, box.max.z+18); scene.add(lz);

  // tick numbers in world mm along all three axes (match sheet.py slice coords)
  const tick = small ? 10 : 50;
  for (let x = Math.ceil(gx0/tick)*tick; x <= gx1 + 0.1; x += tick){
    const l = label(''+Math.round(x), '#8f97a3', step*1.5);
    l.position.set(x, gy0 - step*1.6, z0 + 0.5); scene.add(l);
  }
  for (let y = Math.ceil(gy0/tick)*tick; y <= gy1 + 0.1; y += tick){
    const l = label(''+Math.round(y), '#8f97a3', step*1.5);
    l.position.set(gx0 - step*1.6, y, z0 + 0.5); scene.add(l);
  }
  const zticks = [];
  for (let z = Math.ceil((z0 + 0.05)/tick)*tick; z <= box.max.z + 0.1; z += tick){
    zticks.push(gx0, gy0, z, gx0 - step*0.8, gy0 - step*0.8, z);
    const l = label(''+Math.round(z), '#9bc0ff', step*1.5);
    l.position.set(gx0 - step*1.9, gy0 - step*1.9, z); scene.add(l);
  }
  scene.add(lineSeg(zticks, 0x6ba8ff, 0.9));
  const dims = label(`${size.x.toFixed(0)}×${size.y.toFixed(0)}×${size.z.toFixed(0)}mm`,
                     '#aeb6c2', step*2.6);
  dims.position.set(center.x, gy0 - step*2, z0 + 1); scene.add(dims);

  // clip UI: double-ended slider per axis (keep the slab between the handles)
  const cp = document.getElementById('clips');
  AXES.forEach((ax, i) => {
    const mn = box.min.getComponent(i) - 0.5, mx = box.max.getComponent(i) + 0.5;
    clipState[i].lo = mn; clipState[i].hi = mx;
    const row = document.createElement('div'); row.className = 'cliprow';
    row.innerHTML = `<label><input type="checkbox" id="c${ax}"> clip ${ax}
      <span id="v${ax}" style="opacity:.7; margin-left:auto"></span></label>
      <div class="dslider">
        <input type="range" id="lo${ax}" min="${mn}" max="${mx}" step="${(mx-mn)/400}" value="${mn}">
        <input type="range" id="hi${ax}" min="${mn}" max="${mx}" step="${(mx-mn)/400}" value="${mx}">
      </div>`;
    cp.appendChild(row);
    const apply = () => {
      const st = clipState[i];
      clipPlanes[2*i].constant   = st.on ? -st.lo : 1e6;
      clipPlanes[2*i+1].constant = st.on ?  st.hi : 1e6;
      document.getElementById('v'+ax).textContent =
        st.on ? `${st.lo.toFixed(1)} … ${st.hi.toFixed(1)} mm` : '';
    };
    const loEl = document.getElementById('lo'+ax), hiEl = document.getElementById('hi'+ax);
    document.getElementById('c'+ax).onchange = e => { clipState[i].on = e.target.checked; apply(); };
    loEl.oninput = e => { clipState[i].lo = Math.min(+e.target.value, clipState[i].hi);
                          e.target.value = clipState[i].lo; apply(); };
    hiEl.oninput = e => { clipState[i].hi = Math.max(+e.target.value, clipState[i].lo);
                          e.target.value = clipState[i].hi; apply(); };
    apply();
  });

  const r = Math.max(size.x, size.y, size.z);
  controls.target.copy(center);
  camera.position.set(center.x - r*0.9, center.y - r*1.4, center.z + r*1.1);
  camera.near = r/100; camera.far = r*40; camera.updateProjectionMatrix();
  key.position.set(center.x - r, center.y - r*0.8, center.z + r*2.2);
  key.target.position.copy(center);
  Object.assign(key.shadow.camera, { near:1, far:r*8, left:-r*1.5, right:r*1.5, top:r*1.5, bottom:-r*1.5 });
  key.shadow.camera.updateProjectionMatrix();   // without this the shadow frustum stays at the tiny default
  fill.position.set(center.x + r*1.5, center.y + r, center.z + r);
  controls.update();
});

addEventListener('resize', () => {
  camera.aspect = innerWidth/innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});
(function loop(){ requestAnimationFrame(loop);
  explodeAmt += (explodeTarget - explodeAmt) * 0.12;
  for (const o of pieces){
    const f = o.userData;
    o.position.set(f.base.x + f.off.x*explodeAmt,
                   f.base.y + f.off.y*explodeAmt,
                   f.base.z + f.off.z*explodeAmt);
  }
  controls.update(); renderer.render(scene, camera);
})();

const btn = document.getElementById('spin');
const sync = () => btn.textContent = controls.autoRotate ? 'stop rotation' : 'start rotation';
btn.onclick = () => { controls.autoRotate = !controls.autoRotate;
  prefs.autoRotate = controls.autoRotate; savePrefs(); sync(); };
addEventListener('keydown', e => { if (e.code === 'Space'){ e.preventDefault(); btn.click(); }});
sync();
const exb = document.getElementById('explode');
const exsync = () => exb.textContent = explodeTarget > 0.5 ? 'assemble' : 'explode';
exb.onclick = () => { explodeTarget = explodeTarget > 0.5 ? 0 : 1; exsync(); };
exsync();
</script></body></html>"""


if __name__ == "__main__":
    main()
