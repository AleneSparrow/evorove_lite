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
        columns = page.locator('.feature-grid').evaluate(
            '(grid) => getComputedStyle(grid).gridTemplateColumns.split(" ").filter(Boolean).length')
        assert columns == (1 if width <= 720 else 2), (width, columns)


def _paint(page, locator):
    return locator.evaluate('''(el) => {
      const style = getComputedStyle(el);
      return {color: style.color, background: style.backgroundColor};
    }''')


def test_primary_button_text_differs_from_its_background(page, live_app):
    base, _, _ = live_app
    page.goto(base + '/', wait_until='networkidle')
    for width in (1440, 390):
        page.set_viewport_size({'width': width, 'height': 900})
        buttons = page.locator('a.ev-btn-primary, button.ev-btn-primary, a.button:not(.secondary)')
        assert buttons.count() >= 4
        for index in range(buttons.count()):
            button = buttons.nth(index)
            resting = _paint(page, button)
            assert resting['color'] != resting['background'], (width, index, resting)
            assert resting['color'] == 'rgb(7, 6, 4)', (width, index, resting)
            assert resting['background'] == 'rgb(255, 77, 26)', (width, index, resting)
            button.hover()
            hovered = _paint(page, button)
            assert hovered['color'] != hovered['background'], (width, index, hovered)
            assert hovered['color'] == 'rgb(7, 6, 4)', (width, index, hovered)
            assert hovered['background'] == 'rgb(224, 68, 23)', (width, index, hovered)
