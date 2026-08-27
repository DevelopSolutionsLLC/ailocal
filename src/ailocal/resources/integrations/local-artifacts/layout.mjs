/* layout.mjs — deterministic layout for architecture artifacts.
 *
 * Reads a sized graph on stdin, writes absolute geometry on stdout. Runs in
 * Node inside the TRUSTED server process; artifact code never executes it and
 * gains no privileges from it. elkjs needs no DOM, so no browser is involved.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
const here = dirname(fileURLToPath(import.meta.url));
const { default: ELK } = await import(join(here, "vendor", "elk.bundled.js"));

const input = JSON.parse(readFileSync(0, "utf8"));
const result = await new ELK().layout(input);

/* Absolute node positions, plus each node's ancestor chain. */
const abs = new Map();      // id -> {x,y,width,height,isGroup}
const chain = new Map();    // id -> [ancestor ids, outermost first]
(function walkNodes(n, ox, oy, anc) {
  for (const c of n.children || []) {
    const x = ox + (c.x || 0), y = oy + (c.y || 0);
    abs.set(c.id, { id: c.id, x, y, width: c.width, height: c.height,
                    isGroup: !!(c.children && c.children.length) });
    chain.set(c.id, anc);
    walkNodes(c, x, y, [...anc, c.id]);
  }
})(result, 0, 0, []);

/* ELK expresses an edge's section coordinates relative to the LOWEST COMMON
 * ANCESTOR of its endpoints -- NOT relative to whichever node's `edges` array
 * the JSON happens to carry it in. Reading the offset from the tree position
 * therefore draws every intra-group edge at the wrong place. Resolve the LCA
 * explicitly instead. */
function lcaOffset(srcId, dstId) {
  const a = chain.get(srcId) || [], b = chain.get(dstId) || [];
  let common = null;
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    if (a[i] === b[i]) common = a[i]; else break;
  }
  if (!common) return { x: 0, y: 0 };
  const c = abs.get(common);
  return c ? { x: c.x, y: c.y } : { x: 0, y: 0 };
}

const edges = [];
(function walkEdges(n) {
  for (const e of n.edges || []) {
    const off = lcaOffset((e.sources || [])[0], (e.targets || [])[0]);
    const pts = [];
    for (const s of e.sections || []) {
      pts.push([off.x + s.startPoint.x, off.y + s.startPoint.y]);
      for (const b of s.bendPoints || []) pts.push([off.x + b.x, off.y + b.y]);
      pts.push([off.x + s.endPoint.x, off.y + s.endPoint.y]);
    }
    const l = (e.labels || [])[0];
    edges.push({ id: e.id, points: pts,
                 label: l ? { x: off.x + l.x, y: off.y + l.y,
                              width: l.width, height: l.height } : null });
  }
  for (const c of n.children || []) walkEdges(c);
})(result);

process.stdout.write(JSON.stringify({
  width: result.width, height: result.height,
  nodes: [...abs.values()], edges,
}));
