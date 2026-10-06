"""README figures: two service principals, then the provision order.

    python3 figures/build_readme_figures.py --png
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.expanduser("~/.cursor/skills/architecture-diagrams-figma/scripts"))

from archdiag import (  # noqa: E402
    BODY, DARK_MUTED, DARK_TEXT, FAINT, HAIR, INK, LINE, MONO, MUTED, PAPER, SANS, WASH,
    ZONES, Diagram, badge, card, cross, header, label, mix, paragraph, pill, zone,
)

HERE = os.path.dirname(os.path.abspath(__file__))
W = 1600


def fig_two_principals():
    H = 780
    d = Diagram(W, H, "Fig 1 — Two service principals")
    header(
        d, "Fig. 1 · Two machine identities",
        "The BFF never connects to Lakebase",
        "Two Databricks service principals. Two jobs. Mixing them is how RLS disappears.",
    )

    zone(d, 64, 216, 368, 460, "Public BFF", "azure")
    zone(d, 464, 216, 680, 460, "Databricks workspace", "databricks")
    zone(d, 1176, 216, 360, 460, "Lakebase", "lakebase")

    y = card(d, 88, 288, 320, 220, "Public BFF", "FastAPI · HTTPS", None, "azure")
    with d.group("BFF SP"):
        pill(d, 108, y + 8, "BFF service principal", "azure", size=12)
        d.text(108, y + 64, "Holds client_id + secret.", 13.5, 400, MUTED)
        d.text(108, y + 84, "Calls the app over M2M OAuth.", 13.5, 400, MUTED)
        d.text(108, y + 104, "No Lakebase credentials.", 13.5, 500, BODY)

    gx, gy, gw, gh = 488, 272, 300, 168
    card(d, gx, gy, gw, gh, "CAN_USE only", "Apps ingress", None, "databricks", dark=True, title_size=18)
    with d.group("Ingress"):
        d.text(gx + 20, gy + 118, "Admits the BFF SP.", 13, 400, DARK_TEXT)
        d.text(gx + 20, gy + 138, "Consumer tokens die here.", 13, 400, DARK_MUTED)

    y = card(d, 824, 288, 288, 220, "Databricks App", "Your FastAPI", None, "databricks")
    with d.group("App SP"):
        pill(d, 844, y + 8, "App service principal", "databricks", size=12)
        d.text(844, y + 64, "Injected by Apps.", 13.5, 400, MUTED)
        d.text(844, y + 84, "Connects to Postgres.", 13.5, 400, MUTED)
        d.text(844, y + 104, "bypassrls = false", 13.5, 500, BODY, MONO)

    y = card(d, 1200, 288, 312, 220, "One shared database", "Postgres · RLS", None, "lakebase")
    with d.group("LB"):
        d.text(1220, y + 28, "App SP role only.", 13.5, 400, MUTED)
        d.text(1220, y + 50, "set_config(app.user_id)", 13, 500, INK, MONO)
        d.text(1220, y + 78, "Jobs use a different role", 13.5, 400, MUTED)
        d.text(1220, y + 98, "that may bypass RLS.", 13.5, 400, MUTED)

    d.arrow([(408, 398), (gx, 398)], color=INK, name="BFF to ingress")
    label(d, 436, 378, "BFF SP", bg=ZONES["azure"][1], size=12, mono=True, color=ZONES["azure"][0])
    d.arrow([(gx + gw, 356), (824, 356)], color=INK, name="ingress to app")
    d.arrow([(1112, 398), (1200, 398)], color=INK, name="app to lakebase")
    label(d, 1156, 378, "App SP", bg=ZONES["databricks"][1], size=12, mono=True, color=ZONES["databricks"][0])

    # forbidden path
    d.arrow([(248, 508), (248, 560), (1356, 560), (1356, 508)], color=FAINT, dash="5 6",
            name="Forbidden BFF to Lakebase")
    cross(d, 800, 560)
    pill(d, 800, 588, "No path. BFF has no Postgres role.", "stop", size=12.5, anchor="middle", height=28)

    d.caption(1, "Two service principals")
    return d


def fig_provision_order():
    H = 760
    d = Diagram(W, H, "Fig 2 — Provision order")
    header(
        d, "Fig. 2 · Stand it up in this order",
        "App identity first, then the BFF that is allowed to call it",
        "CAN_USE is the last grant, not the first. Confirm the ACL before you ship a header.",
    )

    steps = [
        ("databricks", "01", "Databricks App", "Deploy the app. Apps injects its own service principal."),
        ("lakebase", "02", "App Postgres role", "create-role as SERVICE_PRINCIPAL. Confirm bypassrls is false."),
        ("azure", "03", "BFF service principal", "Create a second SP. Mint an OAuth secret. Never reuse the app SP."),
        ("databricks", "04", "Grant CAN_USE", "update-permissions on the app. BFF SP plus admins. Nothing else."),
        ("azure", "05", "Point the BFF", "DATABRICKS_HOST, APP_URL, SP id/secret, SESSION_SECRET."),
    ]
    x = 64
    for kind, num, title, body in steps:
        accent, tint, soft = ZONES[kind]
        with d.group(f"Step {num}"):
            d.rect(x, 248, 288, 196, 16, fill=PAPER, stroke=LINE, name="Card")
            d.rect(x, 248, 6, 196, 3, fill=accent, name="Rail")
            d.circle(x + 36, 284, 16, fill=accent, name="Num chip")
            d.text(x + 36, 290, num, 12, 600, "#FFFFFF", MONO, anchor="middle", name="Num")
            d.text(x + 64, 292, title, 16, 600, INK, name="Title")
            paragraph(d, x + 24, 328, body, 240, size=13.5, color=MUTED, lh=1.4, name="Body")
        x += 306

    with d.group("Check"):
        d.rect(64, 476, 1472, 176, 16, fill=WASH, stroke=HAIR, name="Check card")
        d.text(88, 516, "BEFORE YOU TRUST THE HEADER", 11, 500, MUTED, MONO, ls=1.3)
        d.text(88, 552, "databricks apps get-permissions <app-name> --profile <PROFILE>", 14, 500, INK, MONO)
        paragraph(
            d, 88, 580,
            "The list should be: admins CAN_MANAGE, maybe you CAN_MANAGE, and the BFF service principal CAN_USE. "
            "If All account users can use the app, the trusted header is no longer trusted.",
            1424, size=14, color=BODY, lh=1.4, name="Check copy",
        )

    d.caption(2, "Provision order")
    return d


FIGURES = {
    "fig1-two-service-principals": fig_two_principals,
    "fig2-provision-order": fig_provision_order,
}


def main():
    png = "--png" in sys.argv
    os.makedirs(os.path.join(HERE, "svg"), exist_ok=True)
    os.makedirs(os.path.join(HERE, "png"), exist_ok=True)
    for slug, fn in FIGURES.items():
        d = fn()
        out = os.path.join(HERE, "svg", f"{slug}.svg")
        d.save(out)
        print("wrote", out)
        if png:
            p = os.path.join(HERE, "png", f"{slug}.preview.png")
            subprocess.run(["rsvg-convert", "-z", "1", "-o", p, out], check=True)
            print("  preview", p)


if __name__ == "__main__":
    main()
