from playwright.sync_api import expect

from app.db import connect


def register(page, base):
    page.goto(base + '/signup')
    page.get_by_label('Business name', exact=True).fill('Browser acceptance')
    page.get_by_label('Email', exact=True).fill('browser@example.com')
    page.get_by_label('Password', exact=True).fill('Browser test password 123')
    with page.expect_response(lambda r: r.url == base + '/signup' and r.request.method == 'POST') as result:
        page.get_by_role('button', name='Continue', exact=True).click()
    assert result.value.status == 303
    assert result.value.request.headers['origin'] == base
    expect(page).to_have_url(base + '/app')


def test_signup_in_chromium(page, live_app):
    base, _, _ = live_app
    register(page, base)
    expect(page.get_by_role('heading', name='Browser acceptance', exact=True)).to_be_visible()


def test_login_in_chromium(page, live_app):
    base, _, _ = live_app
    register(page, base)
    page.get_by_role('button', name='Log out', exact=True).click()
    expect(page).to_have_url(base + '/login')
    page.get_by_label('Email', exact=True).fill('browser@example.com')
    page.get_by_label('Password', exact=True).fill('Browser test password 123')
    with page.expect_response(lambda r: r.url == base + '/login' and r.request.method == 'POST') as result:
        page.get_by_role('button', name='Continue', exact=True).click()
    assert result.value.status == 303
    assert result.value.request.headers['origin'] == base
    expect(page).to_have_url(base + '/app')


def test_settings_form_in_chromium(page, live_app):
    base, settings, _ = live_app
    register(page, base)
    page.get_by_role('link', name='Business settings', exact=True).click()
    page.get_by_label('Postal address (required)', exact=True).fill('123 Test Street, Austin TX 78701')
    page.get_by_label('Offer text', exact=True).fill('Test offer for browser acceptance')
    page.get_by_label('Search brief', exact=True).fill('US founders with public evidence')
    with page.expect_response(lambda r: r.url == base + '/app/settings' and r.request.method == 'POST') as result:
        page.get_by_role('button', name='Save settings', exact=True).click()
    assert result.value.status == 303
    assert result.value.request.headers['origin'] == base
    expect(page.get_by_role('status')).to_have_text('Settings saved.')
    with connect(settings.db_path) as db:
        row = db.execute('SELECT postal_address,brief FROM businesses').fetchone()
        assert row['postal_address'] == '123 Test Street, Austin TX 78701'
        assert row['brief'] == 'US founders with public evidence'


def test_choose_starter_follows_external_303_without_csp_block(page, live_app):
    base, _, calls = live_app
    register(page, base)
    violations = []
    page.on('console', lambda msg: violations.append(msg.text) if msg.type == 'error' else None)
    # Test-only CONNECT proxy maps this external origin to the local TLS server.
    with page.expect_response(lambda r: r.url == base + '/billing/checkout/starter') as result:
        page.get_by_role('button', name='Choose Starter', exact=True).click()
    assert result.value.status == 303
    assert result.value.headers['location'] == 'https://sandbox.lemonsqueezy.com/checkout/acceptance'
    expect(page).to_have_url('https://sandbox.lemonsqueezy.com/checkout/acceptance')
    expect(page.get_by_role('heading', name='Test checkout destination')).to_be_visible()
    assert len(calls) == 1
    assert calls[0]['data']['relationships']['variant']['data']['id'] == '100'
    assert not any('form-action' in text or 'Content Security Policy' in text for text in violations)
