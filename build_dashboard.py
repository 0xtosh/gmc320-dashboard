"""CLI: regenerate data/dashboard.html (static, no live panel) from data/history.csv."""
from dashboard import CSV_PATH, DATA_DIR, render_html

OUT_PATH = DATA_DIR / "dashboard.html"


def main():
    html = render_html(CSV_PATH, live_enabled=False)
    DATA_DIR.mkdir(exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
