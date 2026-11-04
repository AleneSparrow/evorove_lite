"""Landing night theme: no horizontal overflow, hero clear of the radar."""

WIDTHS = (320, 390, 768, 1024, 1440)


def _metrics(page):
    return page.evaluate('''() => {
      const h1 = document.querySelector('.ev-hero-h1');
      const radar = document.querySelector('.ev-radar');
      const a = h1.getBoundingClientRect();
      const b = radar.getBoundingClientRect();
      const overlap = a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
      return {
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
        overlap,
        fontSize: parseFloat(getComputedStyle(h1).fontSize),
        zoom: parseFloat(getComputedStyle(document.querySelector('.ev-board-scale')).zoom),
      };
    }''')


def test_landing_widths_fit_and_hero_clears_radar(page, live_app):
    base, _, _ = live_app
    page.goto(base + '/', wait_until='networkidle')
    expected_zoom = {320: .19, 390: .24, 768: .42, 1024: .6, 1440: .82}
    for width in WIDTHS:
        page.set_viewport_size({'width': width, 'height': 900})
        metrics = _metrics(page)
        assert metrics['innerWidth'] == width
        assert metrics['scrollWidth'] == metrics['innerWidth'], metrics
        assert metrics['overlap'] is False, metrics
        if width <= 600:
            expected = max(84, min(140, width * .26))
        else:
            expected = max(84, min(168, width * .13))
        assert abs(metrics['fontSize'] - expected) < 1.5, metrics
        assert abs(metrics['zoom'] - expected_zoom[width]) < .011, metrics
