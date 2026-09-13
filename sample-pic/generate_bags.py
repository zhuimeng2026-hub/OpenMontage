"""Sample batch generator — 10 Western-style bag themes × 8 shots each = 80 images.

⚠️  AD-HOC SAMPLE BATCH (not a production pipeline)
This script bypasses the standard pipeline_defs/* workflow because the user
asked for a one-off batch into `projects/sample-pic/` for visual evaluation.
For production work, build a real pipeline_defs entry instead.

Output structure (per theme):
  /opt/OpenMontage/projects/sample-pic/<slug>/
    prompts.md           — all 8 prompts in English + Chinese summary
    01_hero.png          — generated images (MiniMax Image-01)
    02_product.png
    03_detail.png
    04_lifestyle.png
    05_interior.png
    06_material.png
    07_packaging.png
    08_closing.png
    _generation_log.json — per-image status, cost, duration

Total cost est: 80 × $0.003 ≈ $0.24 (unverified — see SKILL.md).
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Ensure we run from the project root so the venv + tools package resolve.
PROJECT_ROOT = Path("/opt/OpenMontage")
sys.path.insert(0, str(PROJECT_ROOT))

from tools.graphics.minimax_image import MiniMaxImage  # noqa: E402

OUTPUT_ROOT = Path("/opt/OpenMontage/projects/sample-pic")

# ------------------------------------------------------------------
# Product definitions — consistent identity per group, varied across groups.
# Each product's "identity" block is prepended to every prompt so the model
# generates the same product across all 8 shots of the group.
# ------------------------------------------------------------------

PRODUCTS = [
    {
        "slug": "01-heritage-leather-briefcase",
        "name_en": "Heritage Leather Briefcase",
        "name_zh": "复古真皮公文包",
        "identity": (
            "A cognac-brown full-grain Italian leather briefcase with brass "
            "buckles, hand-stitched seams, two top handles and a removable "
            "shoulder strap, dark brown suede interior lining, no visible "
            "brand logos, dimensions 16x12x4 inches"
        ),
        "model": (
            "an American businessman in his 40s wearing a tailored navy suit, "
            "white shirt, no tie, neatly trimmed beard, Caucasian features"
        ),
        "context": (
            "Manhattan financial district corner office at golden hour, "
            "floor-to-ceiling windows with city skyline, warm wood desk"
        ),
    },
    {
        "slug": "02-parisian-canvas-tote",
        "name_en": "Parisian Canvas Tote",
        "name_zh": "巴黎帆布托特包",
        "identity": (
            "A natural beige heavyweight canvas tote with vegetable-tanned "
            "tan leather top handles, gold-tone D-ring hardware, removable "
            "zippered pouch inside, dimensions 14x16x5 inches, no logos"
        ),
        "model": (
            "a French woman in her early 30s with shoulder-length wavy brunette "
            "hair, wearing a striped Breton top, high-waisted jeans and "
            "espadrilles, natural minimal makeup"
        ),
        "context": (
            "a sidewalk terrace of a classic Parisian café on a spring morning, "
            "wicker chairs, espresso cup on the marble table"
        ),
    },
    {
        "slug": "03-designer-crossbody",
        "name_en": "Designer Crossbody",
        "name_zh": "设计师斜挎包",
        "identity": (
            "A black nappa leather saddle-shaped crossbody bag with antique "
            "gold chain strap, equestrian-inspired silhouette, front flap "
            "with hidden magnetic closure, dimensions 9x7x3 inches, no logos"
        ),
        "model": (
            "an Italian woman in her late 20s with sleek black bob haircut, "
            "wearing an oversized camel cashmere coat, black leather ankle "
            "boots, dark sunglasses, effortlessly chic"
        ),
        "context": (
            "Via Montenapoleone in Milan during fashion week, sunlit cobblestones, "
            "shallow depth of field, other pedestrians blurred in background"
        ),
    },
    {
        "slug": "04-heritage-steamer-trunk",
        "name_en": "Heritage Steamer Trunk",
        "name_zh": "复古蒸汽箱旅行箱",
        "identity": (
            "A vintage-style steamer trunk covered in dark walnut-brown "
            "embossed canvas with brass corner protectors, leather carry "
            "handles, brass lock and key, classic travel-sticker patina, "
            "dimensions 32x18x16 inches, no modern logos"
        ),
        "model": (
            "an older American gentleman in his 60s with silver hair and "
            "round tortoiseshell glasses, wearing a Harris Tweed blazer, "
            "white shirt, burgundy knit tie, polished oxford shoes"
        ),
        "context": (
            "the lobby of a grand European hotel, marble floors, crystal "
            "chandeliers, art deco elevator doors, late afternoon light"
        ),
    },
    {
        "slug": "05-tech-commuter-backpack",
        "name_en": "Tech Commuter Backpack",
        "name_zh": "城市通勤背包",
        "identity": (
            "A storm-grey ballistic nylon commuter backpack with magnetic "
            "top flap, padded 16-inch laptop compartment, reflective trim, "
            "ergonomic padded shoulder straps, dimensions 18x13x6 inches, "
            "no logos"
        ),
        "model": (
            "a Dutch woman in her early 30s with straight blonde hair in "
            "a low ponytail, wearing a charcoal wool coat, white shirt, "
            "navy trousers, riding a black city bicycle"
        ),
        "context": (
            "Amsterdam cobblestone canal-side street at dawn, traditional "
            "Dutch gabled houses in soft morning light, misty atmosphere"
        ),
    },
    {
        "slug": "06-evening-clutch",
        "name_en": "Evening Clutch",
        "name_zh": "金属晚宴手包",
        "identity": (
            "A polished silver-tone metal-frame clutch with a structured "
            "rectangular silhouette, clasp closure, optional thin chain "
            "wristlet, satin interior lining, dimensions 8x4x2 inches, "
            "no logos"
        ),
        "model": (
            "a British woman in her late 40s with a short elegant auburn "
            "bob, wearing a floor-length black crepe evening gown, "
            "diamond drop earrings, soft glamour makeup"
        ),
        "context": (
            "the grand staircase of a Viennese opera house at intermission, "
            "warm chandelier light, ornate gilded architecture"
        ),
    },
    {
        "slug": "07-weekend-duffle",
        "name_en": "Weekend Duffle",
        "name_zh": "周末旅行包",
        "identity": (
            "A tan waxed canvas and saddle-leather duffle bag with antique "
            "brass YKK zippers, rolled leather top handles, removable "
            "shoulder strap, dimensions 22x12x10 inches, no logos"
        ),
        "model": (
            "an American couple in their early 30s, the woman with wavy "
            "chestnut hair in a cream knit sweater, the man in a flannel "
            "shirt and jeans, both relaxed and smiling"
        ),
        "context": (
            "a stone cottage doorstep in the Cotswolds on an overcast autumn "
            "afternoon, fallen leaves, an old Land Rover in the background"
        ),
    },
    {
        "slug": "08-iconic-flap-handbag",
        "name_en": "Iconic Flap Handbag",
        "name_zh": "经典绗缝翻盖手袋",
        "identity": (
            "A black lambskin leather quilted-diamond flap handbag with "
            "antique gold-tone chain-and-leather interwoven strap, turn-lock "
            "closure, dimensions 10x7x3 inches, no logos"
        ),
        "model": (
            "a French woman in her mid-20s with long dark hair, wearing a "
            "tailored black blazer, white tee, straight-leg jeans, gold "
            "hoop earrings, understated Parisian style"
        ),
        "context": (
            "outside a fashion-week venue on Avenue Montaigne, Paris, "
            "photographer's POV, blurred street scene, soft daylight"
        ),
    },
    {
        "slug": "09-aluminum-carry-on",
        "name_en": "Aluminum Carry-On",
        "name_zh": "铝镁合金登机箱",
        "identity": (
            "A brushed silver anodized aluminum hard-shell carry-on suitcase "
            "with TSA-approved combination lock, leather carry handle, "
            "Japanese Hinomoto silent spinner wheels, dimensions 22x14x9 "
            "inches, no logos"
        ),
        "model": (
            "an American businesswoman in her late 30s with a sleek "
            "chignon, wearing a camel cashmere wrap coat, black tailored "
            "trousers, low pumps, confident posture"
        ),
        "context": (
            "the interior of an airport VIP lounge, floor-to-ceiling windows, "
            "a glass of champagne, soft golden afternoon light"
        ),
    },
    {
        "slug": "10-streetwear-belt-bag",
        "name_en": "Streetwear Belt Bag",
        "name_zh": "街头风腰带包",
        "identity": (
            "A matte black Cordura nylon belt bag with water-resistant "
            "YKK Aquaguard zipper, adjustable webbing strap, dimensions "
            "9x5x3 inches, reflective trim, no logos"
        ),
        "model": (
            "an American male in his mid-20s with curly brown hair, wearing "
            "an oversized vintage denim jacket, white tee, black cargo pants, "
            "and chunky white sneakers"
        ),
        "context": (
            "an industrial Brooklyn loft rooftop at sunset, exposed brick, "
            "string lights, downtown skyline in soft focus"
        ),
    },
]

# 8 shot templates — each combines the product identity with a shot purpose.
# Model inclusion rules:
#   - shots 1, 2, 3, 5, 6, 7, 8 → product-focused (no model, or model off-camera)
#   - shot 4 (lifestyle) → WITH Western model pairing (per user request)
SHOTS = [
    {
        "key": "01_hero",
        "name": "Hero Lifestyle",
        "purpose": "campaign opener / cover image",
        "aspect": "16:9",
        "include_model": False,
        "template": (
            "Premium commercial product photography, {identity}, as the hero "
            "subject of {context}, soft cinematic light, shallow depth of "
            "field, restrained luxury color palette, {ratio} aspect ratio, "
            "no visible brand logos, no readable text"
        ),
    },
    {
        "key": "02_product",
        "name": "Product Main",
        "purpose": "primary e-commerce product image",
        "aspect": "1:1",
        "include_model": False,
        "template": (
            "Clean premium e-commerce product photography, {identity}, "
            "three-quarter front angle, perfectly centered, soft seamless "
            "light grey background, subtle floor shadow, studio softbox "
            "lighting, sharp focus on hardware and seams, {ratio}, no "
            "text no logos no watermarks"
        ),
    },
    {
        "key": "03_detail",
        "name": "Detail Close-Up",
        "purpose": "highlight craftsmanship / hardware",
        "aspect": "4:3",
        "include_model": False,
        "template": (
            "Extreme close-up commercial macro photography of {identity}, "
            "focus on the hardware, stitching or material texture, 85mm "
            "macro lens, very shallow depth of field, neutral studio "
            "lighting, {ratio}, no text no logos"
        ),
    },
    {
        "key": "04_lifestyle",
        "name": "Lifestyle with Model",
        "purpose": "real-world use, model pairing (per user request)",
        "aspect": "16:9",
        "include_model": True,
        "template": (
            "Lifestyle commercial photography, {identity}, carried or worn "
            "naturally by {model}, set in {context}, documentary-style "
            "authentic moment, product clearly visible and in focus, "
            "warm natural light, {ratio}, no readable text no logos"
        ),
    },
    {
        "key": "05_interior",
        "name": "Interior / Capacity",
        "purpose": "show internal organization",
        "aspect": "4:3",
        "include_model": False,
        "template": (
            "Premium product still-life, {identity} opened and laid flat on "
            "a pale linen surface, full interior visible with organized "
            "compartments, subtle prop items such as folded scarf, leather "
            "cardholder or sunglasses placed nearby, soft overhead light, "
            "{ratio}, no text no logos"
        ),
    },
    {
        "key": "06_material",
        "name": "Material Texture",
        "purpose": "highlight material quality and durability",
        "aspect": "16:9",
        "include_model": False,
        "template": (
            "High-detail commercial material photography of {identity}, "
            "placed on a dark slate-grey studio plinth, strong but soft "
            "directional side light raking across the surface to reveal "
            "material grain and finish, restrained palette, {ratio}, no "
            "text no logos"
        ),
    },
    {
        "key": "07_packaging",
        "name": "Packaging / Unboxing",
        "purpose": "show packaging and accessories",
        "aspect": "3:2",
        "include_model": False,
        "template": (
            "Premium brand unboxing still-life, {identity} beside an open "
            "cream-colored rigid gift box with tissue paper, dust bag, "
            "care card and small leather hangtag neatly arranged, soft "
            "morning window light, slight overhead angle, {ratio}, no "
            "readable text no logos no barcodes"
        ),
    },
    {
        "key": "08_closing",
        "name": "Closing CTA",
        "purpose": "final ad / CTA board",
        "aspect": "16:9",
        "include_model": False,
        "template": (
            "Minimal high-end advertising closing image, {identity} standing "
            "on a deep charcoal gradient background, dramatic top-down "
            "light revealing the silhouette, soft floor shadow, product "
            "positioned left-of-center with generous negative space on the "
            "right for text overlay, {ratio}, no text no logos no QR codes"
        ),
    },
]


def build_prompt(product: dict, shot: dict) -> str:
    """Compose one prompt by interpolating product identity + shot template."""
    fields = {
        "identity": product["identity"],
        "context": product["context"],
        "model": product.get("model", ""),
        "ratio": shot["aspect"],
    }
    return shot["template"].format(**fields).strip()


def render_prompts_markdown(products: list) -> str:
    """Build a single markdown document containing every prompt — saved
    to each group's directory so the human can review what was sent."""
    out = ["# 10 Western-Style Bag Themes — Generated Prompts\n"]
    out.append("Generated by `/opt/OpenMontage/sample-pic/generate_bags.py` "
               "on " + time.strftime("%Y-%m-%d %H:%M:%S") + ".\n")
    out.append("Model: `minimax/image-01` via direct API. "
               "Each group uses an 8-shot template "
               "(`01_hero` → `08_closing`).\n\n")
    for product in products:
        out.append(f"## {product['slug']} — {product['name_en']} / {product['name_zh']}\n\n")
        out.append(f"**Identity:** {product['identity']}\n\n")
        if product.get("model"):
            out.append(f"**Model pairing:** {product['model']}\n\n")
        out.append(f"**Context:** {product['context']}\n\n")
        out.append("---\n\n")
        for shot in SHOTS:
            out.append(f"### {shot['key']} — {shot['name']} ({shot['aspect']})\n\n")
            out.append(f"**Purpose:** {shot['purpose']}\n\n")
            out.append(f"**Prompt:**\n\n```text\n{build_prompt(product, shot)}\n```\n\n")
        out.append("---\n\n")
    return "".join(out)


def generate_one(product: dict, shot: dict, output_dir: Path) -> dict:
    """Generate a single image. Returns a status dict (no exceptions raised)."""
    out_path = output_dir / f"{shot['key']}.png"
    prompt = build_prompt(product, shot)
    tool = MiniMaxImage()

    start = time.time()
    try:
        result = tool.execute({
            "prompt": prompt,
            "aspect_ratio": shot["aspect"],
            "output_path": str(out_path),
        })
        duration = round(time.time() - start, 2)
        if not result.success:
            return {
                "shot": shot["key"],
                "ok": False,
                "error": result.error,
                "duration_seconds": duration,
            }
        return {
            "shot": shot["key"],
            "ok": True,
            "output": str(out_path),
            "cost_usd": result.cost_usd,
            "duration_seconds": duration,
            "size_bytes": out_path.stat().st_size if out_path.exists() else 0,
        }
    except Exception as e:
        return {
            "shot": shot["key"],
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "duration_seconds": round(time.time() - start, 2),
        }


def generate_group(product: dict, max_workers: int = 4) -> list[dict]:
    """Generate all 8 shots for one product, in parallel."""
    output_dir = OUTPUT_ROOT / product["slug"]
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write the prompts file up front so the human can inspect even if
    # generation fails or is interrupted.
    prompts_md = output_dir / "prompts.md"
    prompts_md.write_text(
        f"# {product['slug']} — {product['name_en']} / {product['name_zh']}\n\n"
        f"**Identity:** {product['identity']}\n\n"
        f"**Model pairing:** {product.get('model', '(none for this group)')}\n\n"
        f"**Context:** {product['context']}\n\n"
        f"---\n\n"
        + "\n\n---\n\n".join(
            f"## {shot['key']} — {shot['name']} ({shot['aspect']})\n\n"
            f"**Purpose:** {shot['purpose']}\n\n"
            f"```text\n{build_prompt(product, shot)}\n```"
            for shot in SHOTS
        ),
        encoding="utf-8",
    )

    # Generate up to 4 shots concurrently (rate-limit safety).
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(generate_one, product, shot, output_dir): shot
            for shot in SHOTS
        }
        for fut in as_completed(futures):
            shot = futures[fut]
            r = fut.result()
            r["shot"] = shot["key"]
            r["purpose"] = shot["purpose"]
            results.append(r)
            status = "OK " if r["ok"] else "FAIL"
            print(f"  [{status}] {product['slug']}/{shot['key']} ({r['duration_seconds']}s)")
            if not r["ok"]:
                print(f"          error: {r['error']}")
    return results


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    # Write the consolidated prompts.md at the top level for easy review.
    (OUTPUT_ROOT / "ALL_PROMPTS.md").write_text(
        render_prompts_markdown(PRODUCTS), encoding="utf-8"
    )
    print(f"Wrote {OUTPUT_ROOT / 'ALL_PROMPTS.md'}")

    overall_start = time.time()
    all_results: dict[str, list[dict]] = {}
    for product in PRODUCTS:
        print(f"\n=== {product['slug']} — {product['name_en']} ===")
        all_results[product["slug"]] = generate_group(product, max_workers=4)

    # Per-group generation log
    total_cost = 0.0
    total_ok = 0
    total_fail = 0
    for slug, results in all_results.items():
        log_path = OUTPUT_ROOT / slug / "_generation_log.json"
        log_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        ok = sum(1 for r in results if r["ok"])
        cost = sum(r.get("cost_usd", 0.0) for r in results if r["ok"])
        total_ok += ok
        total_fail += len(results) - ok
        total_cost += cost

    elapsed = round(time.time() - overall_start, 2)
    summary = {
        "groups": len(PRODUCTS),
        "total_images": len(PRODUCTS) * len(SHOTS),
        "ok": total_ok,
        "failed": total_fail,
        "estimated_cost_usd": round(total_cost, 3),
        "wall_clock_seconds": elapsed,
    }
    (OUTPUT_ROOT / "_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print("\n========== SUMMARY ==========")
    print(json.dumps(summary, indent=2))
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())