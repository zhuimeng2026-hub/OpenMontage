#!/usr/bin/env node
/**
 * Download self-hosted woff2 font files from Google Fonts.
 * Run: node scripts/download-fonts.js
 *
 * Fonts are saved to public/fonts/ with naming convention:
 *   {Family}[-Italic]-wght{NNN}.woff2
 *
 * Must use a Chrome-like User-Agent or Google Fonts returns TTF instead of woff2.
 */
const https = require("https");
const fs = require("fs");
const path = require("path");

const DEST = path.join(__dirname, "..", "public", "fonts");
fs.mkdirSync(DEST, { recursive: true });

const UA =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

function fetch(url) {
  return new Promise((resolve, reject) => {
    https.get(url, { headers: { "User-Agent": UA } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        https.get(res.headers.location, { headers: { "User-Agent": UA } }, resolve).on("error", reject);
        return;
      }
      if (res.statusCode !== 200) {
        reject(new Error(`HTTP ${res.statusCode} for ${url}`));
        return;
      }
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve(Buffer.concat(chunks)));
    }).on("error", reject);
  });
}

async function downloadFamily(family, weights, italic = false) {
  const italicStr = italic ? ":ital,wght@" : ":wght@";
  const weightStr = weights.join(";");
  const url = `https://fonts.googleapis.com/css2?family=${family}${italicStr}${weightStr}&display=swap`;
  console.log(`  Fetching CSS for ${family}${italic ? " italic" : ""}…`);
  const css = (await fetch(url)).toString();
  // Extract all woff2 urls (skip latin-only subset, prefer the largest)
  const matches = [...css.matchAll(/url\((https:\/\/fonts\.gstatic\.com[^)]+\.woff2)\)/g)];
  if (!matches.length) { console.warn(`  WARNING: no woff2 found for ${family}`); return; }

  // Group by weight by reading the font-weight from CSS
  const weightBlocks = {};
  let currentWeight = "400";
  for (const line of css.split("\n")) {
    const wMatch = line.match(/font-weight:\s*(\d+)/);
    if (wMatch) currentWeight = wMatch[1];
    if (line.includes(".woff2")) {
      const m = line.match(/url\((https:\/\/fonts\.gstatic\.com[^)]+\.woff2)\)/);
      if (m && !weightBlocks[currentWeight]) weightBlocks[currentWeight] = m[1];
    }
  }

  for (const [w, url] of Object.entries(weightBlocks)) {
    const name = `${family.replace(/\s+/g, "")}${italic ? "-Italic" : ""}-wght${w}.woff2`;
    const outPath = path.join(DEST, name);
    if (fs.existsSync(outPath)) { console.log(`  ${name} already exists, skipping`); continue; }
    console.log(`  ${name}…`);
    try {
      const buf = await fetch(url);
      fs.writeFileSync(outPath, buf);
      console.log(`    ✓ ${buf.length} B`);
    } catch (e) {
      console.error(`    ✗ ${e.message}`);
    }
  }
}

async function main() {
  console.log("Downloading fonts to", DEST);
  await downloadFamily("Inter",             [300, 400, 500, 600, 700, 800, 900]);
  await downloadFamily("Space Grotesk",     [300, 400, 500, 600, 700]);
  await downloadFamily("JetBrains Mono",     [400, 500, 600, 700]);
  await downloadFamily("Playfair Display",   [400, 500, 600, 700, 800, 900]);
  await downloadFamily("Playfair Display",   [400, 500, 600, 700], true);
  const files = fs.readdirSync(DEST).sort();
  const total = files.reduce((s, f) => s + fs.statSync(path.join(DEST, f)).size, 0);
  console.log(`\nDone: ${files.length} files, ${(total / 1024).toFixed(0)} KB total`);
  console.log(files.join("\n"));
}

main().catch((e) => { console.error(e); process.exit(1); });
