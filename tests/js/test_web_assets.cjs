'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');
const JS_DIR = path.join(ROOT, 'tweetxvault', 'web', 'static', 'js');

function browserContext() {
    const stored = new Map();
    const cssProperties = new Map();
    const rootClasses = new Set();
    const events = new Map();
    const historyCalls = [];
    const scrollCalls = [];
    const appendedLinks = [];
    const videos = [];

    const context = {
        console: {
            log() {},
            error() {},
            warn() {},
        },
        setTimeout,
        clearTimeout,
        URL,
        Date,
        Math,
        JSON,
        Promise,
        encodeURIComponent,
        decodeURIComponent,
        confirm: () => true,
        alert() {},
        CustomEvent: class CustomEvent {
            constructor(type, options = {}) {
                this.type = type;
                this.detail = options.detail;
            }
        },
        localStorage: {
            getItem(key) {
                return stored.has(key) ? stored.get(key) : null;
            },
            setItem(key, value) {
                stored.set(key, String(value));
            },
            removeItem(key) {
                stored.delete(key);
            },
        },
        history: {
            scrollRestoration: 'auto',
            replaceState(...args) {
                historyCalls.push(['replace', ...args]);
            },
            pushState(...args) {
                historyCalls.push(['push', ...args]);
            },
            back() {
                historyCalls.push(['back']);
            },
        },
        document: {
            documentElement: {
                style: {
                    setProperty(key, value) {
                        cssProperties.set(key, value);
                    },
                },
                classList: {
                    add(value) {
                        rootClasses.add(value);
                    },
                    remove(value) {
                        rootClasses.delete(value);
                    },
                },
            },
            body: {
                style: {},
                querySelectorAll() {
                    return [];
                },
            },
            head: {
                appendChild(node) {
                    appendedLinks.push(node);
                },
            },
            querySelector(selector) {
                if (selector.startsWith('link[data-font=')) {
                    return appendedLinks.find(link => selector.includes(link.dataset.font)) || null;
                }
                return null;
            },
            querySelectorAll(selector) {
                return selector === 'video' ? videos : [];
            },
            getElementById() {
                return null;
            },
            createElement(tag) {
                return { tagName: tag.toUpperCase(), dataset: {}, style: {} };
            },
            createRange() {
                return {
                    cloneRange() {
                        return this;
                    },
                    selectNodeContents() {},
                    setEnd() {},
                    setStart() {},
                    collapse() {},
                    toString() {
                        return '';
                    },
                };
            },
        },
        IntersectionObserver: class IntersectionObserver {
            constructor(callback) {
                this.callback = callback;
                this.observed = [];
            }
            observe(node) {
                this.observed.push(node);
            }
        },
        MutationObserver: class MutationObserver {
            constructor(callback) {
                this.callback = callback;
            }
            observe() {}
        },
    };

    context.window = {
        innerWidth: 1280,
        scrollY: 0,
        location: { origin: 'http://localhost' },
        addEventListener(name, callback) {
            events.set(name, callback);
        },
        dispatchEvent(event) {
            const callback = events.get(event.type);
            if (callback) callback(event);
        },
        scrollTo(...args) {
            scrollCalls.push(args);
        },
        getSelection() {
            return {
                rangeCount: 0,
                removeAllRanges() {},
                addRange() {},
            };
        },
    };
    context.globalThis = context;
    context.__state = {
        stored,
        cssProperties,
        rootClasses,
        events,
        historyCalls,
        scrollCalls,
        appendedLinks,
        videos,
    };
    vm.createContext(context);
    return context;
}

function loadScripts(context, names, exportExpression) {
    const source = names
        .map(name => fs.readFileSync(path.join(JS_DIR, name), 'utf8'))
        .join('\n');
    vm.runInContext(`${source}\nglobalThis.__exports = ${exportExpression};`, context, {
        filename: names.join('+'),
    });
    return context.__exports;
}

function immediateComponent(component) {
    component.$nextTick = callback => callback();
    component.$watch = () => {};
    component.$refs = {};
    return component;
}

const tests = [];
function test(name, fn) {
    tests.push({ name, fn });
}

test('theme color helpers and catalog generate complete deterministic themes', () => {
    const context = browserContext();
    const exported = loadScripts(
        context,
        ['themes.js'],
        '({_hexToRgb, _rgbToHex, _rgbToHsl, _hslToRgb, _adjustL, _mix, _rgba, _surface, _generateTheme, THEME_SEEDS, THEMES, FONT_URLS})',
    );

    assert.deepEqual(Array.from(exported._hexToRgb('#1d9bf0')), [29, 155, 240]);
    assert.equal(exported._rgbToHex(29, 155, 240), '#1d9bf0');
    const hsl = exported._rgbToHsl(29, 155, 240);
    const rgb = exported._hslToRgb(...hsl);
    assert.equal(exported._rgbToHex(...rgb), '#1d9bf0');
    assert.equal(exported._mix('#000000', '#ffffff', 0.5), '#808080');
    assert.equal(exported._rgba('#010203', 0.5), 'rgba(1,2,3,0.5)');
    assert.match(exported._adjustL('#000000', 10), /^#[0-9a-f]{6}$/);
    assert.match(exported._surface('#000000', '#71767b', 8), /^#[0-9a-f]{6}$/);

    const themeKeys = Object.keys(exported.THEMES);
    assert.ok(themeKeys.length >= 15);
    assert.deepEqual(themeKeys, Object.keys(exported.THEME_SEEDS));
    for (const theme of Object.values(exported.THEMES)) {
        for (const key of [
            '--bg-primary',
            '--bg-secondary',
            '--text-primary',
            '--text-secondary',
            '--border-color',
            '--accent-color',
            '--danger-color',
        ]) {
            assert.ok(theme[key], `missing ${key}`);
        }
    }
    assert.ok(exported.THEMES['classic-dark']._accents.length >= 6);
    assert.match(exported.FONT_URLS.jetbrains, /JetBrains\+Mono/);
});

test('autocomplete formats valid, invalid, negative, quoted, and escaped capsules', () => {
    const context = browserContext();
    const { searchAutocomplete } = loadScripts(
        context,
        ['autocomplete.js'],
        '({searchAutocomplete})',
    );
    const input = { innerHTML: '' };
    const component = immediateComponent(searchAutocomplete());
    component.$refs.searchInput = input;
    component.globalTags = [{ tag: 'Night Sky' }];
    component.knownAuthors = new Set(['alice']);

    component.formatRichText(
        'from:alice -has:video tag:"Night Sky" filter:nope <img src=x>',
    );

    assert.match(input.innerHTML, /valid-capsule/);
    assert.match(input.innerHTML, /negative-capsule/);
    assert.match(input.innerHTML, /hidden-quote/);
    assert.match(input.innerHTML, /invalid-capsule/);
    assert.match(input.innerHTML, /&lt;img/);
    assert.doesNotMatch(input.innerHTML, /<img src=x>/);
});

test('autocomplete highlighting escapes API-provided labels', () => {
    const context = browserContext();
    const { searchAutocomplete } = loadScripts(
        context,
        ['autocomplete.js'],
        '({searchAutocomplete})',
    );
    const component = searchAutocomplete();
    const highlighted = component.highlightMatch('<img onerror=x> Alice', 'ali');
    assert.equal(highlighted, '&lt;img onerror=x&gt; <b>Ali</b>ce');
    assert.equal(component.highlightMatch('<script>', ''), '&lt;script&gt;');
});

test('autocomplete offers the attached-article filter', () => {
    const context = browserContext();
    const { searchAutocomplete } = loadScripts(
        context,
        ['autocomplete.js'],
        '({searchAutocomplete})',
    );
    const component = searchAutocomplete();
    assert.ok(component.filterOptions.some(option => option.value === 'articles'));
});

test('autocomplete keyboard navigation wraps and scrolls selected option', () => {
    const context = browserContext();
    const { searchAutocomplete } = loadScripts(
        context,
        ['autocomplete.js'],
        '({searchAutocomplete})',
    );
    let scrolled = 0;
    const component = immediateComponent(searchAutocomplete());
    component.showDropdown = true;
    component.options = [{}, {}, {}];
    component.$refs.dropdownMenu = {
        querySelectorAll() {
            return component.options.map(() => ({
                scrollIntoView() {
                    scrolled += 1;
                },
            }));
        },
    };

    component.selectedIndex = 2;
    component.moveDown();
    assert.equal(component.selectedIndex, 0);
    component.moveUp();
    assert.equal(component.selectedIndex, 2);
    assert.equal(scrolled, 2);
});

test('autocomplete quotes selected multi-word values and keeps caret position', () => {
    const context = browserContext();
    const { searchAutocomplete } = loadScripts(
        context,
        ['autocomplete.js'],
        '({searchAutocomplete})',
    );
    let selection = null;
    const component = immediateComponent(searchAutocomplete());
    component.searchQuery = 'before tag:ni after';
    component.cursorPos = 'before tag:ni'.length;
    component.showDropdown = true;
    component.options = [{ prefix: 'tag:', value: 'Night Sky' }];
    component.$refs.searchInput = {
        isContentEditable: false,
        focus() {},
        setSelectionRange(start, end) {
            selection = [start, end];
        },
    };
    component.handleInput = () => {};

    component.selectOption();

    assert.equal(component.searchQuery, 'before tag:"Night Sky"  after');
    assert.deepEqual(selection, [23, 23]);
});

test('autocomplete fetches author and tag suggestions with encoded queries', async () => {
    const context = browserContext();
    const calls = [];
    context.fetch = async url => {
        calls.push(url);
        if (url.startsWith('/api/authors')) {
            return {
                ok: true,
                async json() {
                    return {
                        authors: [
                            { id: '1', username: 'Alice', display_name: 'Alice A' },
                        ],
                    };
                },
            };
        }
        return {
            ok: true,
            async json() {
                return { tags: [{ tag: 'Night Sky', count: 1200 }] };
            },
        };
    };
    const { searchAutocomplete } = loadScripts(
        context,
        ['autocomplete.js'],
        '({searchAutocomplete})',
    );
    const component = immediateComponent(searchAutocomplete());
    component.$refs.searchInput = {
        isContentEditable: false,
        selectionStart: 10,
    };

    component.searchQuery = 'from:a b';
    component.cursorPos = component.searchQuery.length;
    component.handleInput();
    component.searchQuery = 'tag:night sky';
    component.cursorPos = component.searchQuery.length;
    component.handleInput();
    await new Promise(resolve => setTimeout(resolve, 0));

    assert.deepEqual(calls, []);

    component.searchQuery = 'from:ali';
    component.cursorPos = component.searchQuery.length;
    component.handleInput();
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(component.options[0].value, 'Alice');
    assert.ok(component.knownAuthors.has('alice'));
    assert.match(calls[0], /q=ali$/);

    component.searchQuery = 'tag:night';
    component.cursorPos = component.searchQuery.length;
    component.handleInput();
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(component.options[0].value, 'Night Sky');
    assert.equal(component.options[0].count, '1,200');
});

test('autocomplete calendar handles leap years and month boundaries', () => {
    const context = browserContext();
    const { searchAutocomplete } = loadScripts(
        context,
        ['autocomplete.js'],
        '({searchAutocomplete})',
    );
    const component = searchAutocomplete();
    component.dpYear = 2024;
    component.dpMonth = 1;
    const days = component.dpGetDays();
    assert.equal(days.length, 42);
    assert.ok(days.some(day => day.isCurrentMonth && day.dateStr === '2024-02-29'));

    component.dpMonth = 0;
    component.dpPrevMonth();
    assert.deepEqual([component.dpYear, component.dpMonth], [2023, 11]);
    component.dpNextMonth();
    assert.deepEqual([component.dpYear, component.dpMonth], [2024, 0]);

    component.initDatePicker('2026-07-30');
    assert.deepEqual([component.dpYear, component.dpMonth], [2026, 6]);
    assert.equal(component.formatDateStr(2026, 7, 3), '2026-07-03');
});

test('tweet app starts with coherent list, panel, modal, and theme state', () => {
    const context = browserContext();
    const { tweetApp } = loadScripts(
        context,
        ['themes.js', 'app.js'],
        '({tweetApp})',
    );
    const app = immediateComponent(tweetApp());
    assert.equal(app.viewMode, 'list');
    assert.equal(app.page, 1);
    assert.equal(app.collectionFilter, 'all');
    assert.equal(app.panelMode, null);
    assert.deepEqual(Array.from(app.panelStack), []);
    assert.equal(app.tagModalOpen, false);
    assert.equal(app.currentTheme, 'classic-dark');
    assert.equal(app.showEmptyUnavailableReasons, false);
    assert.ok(Object.keys(app.THEMES).length >= 15);
});

test('archive status filters empty reasons and summarizes retry state', () => {
    const context = browserContext();
    const { tweetApp } = loadScripts(
        context,
        ['themes.js', 'app.js'],
        '({tweetApp})',
    );
    const app = immediateComponent(tweetApp());
    app.statsHealth = {
        enrichment: {
            unavailable: {
                reasons: [
                    {
                        reason: 'unavailable_unknown',
                        count: 2,
                        percent_of_missing: 66.7,
                        due: 1,
                        delayed: 1,
                        permanent: 0,
                    },
                    {
                        reason: 'deleted_by_author',
                        count: 1,
                        percent_of_missing: 33.3,
                        due: 0,
                        delayed: 0,
                        permanent: 1,
                    },
                    {
                        reason: 'withheld',
                        count: 0,
                        percent_of_missing: 0,
                        due: 0,
                        delayed: 0,
                        permanent: 0,
                    },
                ],
            },
        },
    };

    assert.deepEqual(
        Array.from(app.getUnavailableReasons(), item => item.reason),
        ['unavailable_unknown', 'deleted_by_author'],
    );
    assert.deepEqual(
        Array.from(app.getUnavailableBarReasons(), item => item.reason),
        ['unavailable_unknown', 'deleted_by_author'],
    );
    assert.equal(
        app.getUnavailableReasonStatus(app.statsHealth.enrichment.unavailable.reasons[0]),
        '1 due now · 1 scheduled',
    );
    assert.equal(
        app.getUnavailableReasonStatus(app.statsHealth.enrichment.unavailable.reasons[1]),
        '1 permanent',
    );
    assert.ok(
        app.getUnavailableSegmentWidth(app.statsHealth.enrichment.unavailable.reasons[0])
        > app.getUnavailableSegmentWidth(app.statsHealth.enrichment.unavailable.reasons[1]),
    );

    app.showEmptyUnavailableReasons = true;
    assert.equal(app.getUnavailableReasons().length, 3);
    assert.equal(app.getUnavailableBarReasons().length, 2);
    assert.equal(
        app.getUnavailableReasonStatus(app.statsHealth.enrichment.unavailable.reasons[2]),
        'No unavailable tweets',
    );
});

test('analytics markup exposes the Archive status cards and reason breakdown', () => {
    const html = fs.readFileSync(
        path.join(ROOT, 'tweetxvault', 'web', 'index.html'),
        'utf8',
    );

    assert.match(html, />Archive status</);
    assert.match(html, />Enriched /);
    assert.match(html, />Threads /);
    assert.match(html, />Missing enrichment /);
    assert.match(html, />Resurrected /);
    assert.match(html, />Unavailable tweets /);
    assert.match(html, /x-model="showEmptyUnavailableReasons"/);
    assert.match(html, /archive-status-bar-seg/);
    assert.match(html, /archive-status-reason-row/);
    assert.doesNotMatch(html, />Pipeline health</);
});

test('tweet fetching encodes search state, hydrates pagination, appends, and reports errors', async () => {
    const context = browserContext();
    const calls = [];
    let responseData = {
        tweets: [{ tweet_id: '1' }],
        page: 1,
        pages: 3,
        total: 5,
    };
    context.fetch = async url => {
        calls.push(url);
        return {
            ok: true,
            status: 200,
            async json() {
                return responseData;
            },
        };
    };
    const { tweetApp } = loadScripts(
        context,
        ['themes.js', 'app.js'],
        '({tweetApp})',
    );
    const app = immediateComponent(tweetApp());
    app.collectionFilter = 'likes';
    app.sortOrder = 'relevance';
    app.searchQuery = 'from:alice night sky';

    await app.fetchTweets();
    assert.equal(
        calls[0],
        '/api/tweets?collection=likes&sort=relevance&page=1&q=from%3Aalice%20night%20sky',
    );
    assert.deepEqual(Array.from(app.tweets, tweet => tweet.tweet_id), ['1']);
    assert.equal(app.totalPages, 3);
    assert.equal(app.total, 5);
    assert.equal(app.loading, false);

    responseData = { tweets: [{ tweet_id: '2' }], page: 2, pages: 3, total: 5 };
    app.page = 2;
    await app.fetchTweets(true);
    assert.deepEqual(Array.from(app.tweets, tweet => tweet.tweet_id), ['1', '2']);
    assert.equal(app.loadingMore, false);

    context.fetch = async () => ({
        ok: false,
        status: 503,
        async json() {
            return { detail: 'temporarily unavailable' };
        },
    });
    await app.fetchTweets();
    assert.equal(app.error, 'temporarily unavailable');
    assert.deepEqual(Array.from(app.tweets), []);
});

test('list searching, incremental loading, and back-to-top obey state guards', () => {
    const context = browserContext();
    const { tweetApp } = loadScripts(
        context,
        ['themes.js', 'app.js'],
        '({tweetApp})',
    );
    const app = immediateComponent(tweetApp());
    let fetches = 0;
    app.fetchTweets = append => {
        fetches += append ? 10 : 1;
    };

    app.searchQuery = '';
    app.sortOrder = 'default';
    app.search();
    assert.equal(app.sortOrder, 'newest');
    assert.equal(fetches, 1);

    app.loading = false;
    app.loadingMore = false;
    app.page = 1;
    app.totalPages = 2;
    app.loadMore();
    assert.equal(app.page, 2);
    assert.equal(fetches, 11);
    app.loadMore();
    assert.equal(fetches, 11);

    app.scrollToTop();
    const scrollOptions = context.__state.scrollCalls.at(-1)[0];
    assert.equal(scrollOptions.top, 0);
    assert.equal(scrollOptions.behavior, 'smooth');
});

test('themes, accents, fonts, sizes, density, and split panel persist preferences', () => {
    const context = browserContext();
    const { tweetApp } = loadScripts(
        context,
        ['themes.js', 'app.js'],
        '({tweetApp})',
    );
    const app = immediateComponent(tweetApp());

    app.applyTheme('classic-light');
    assert.equal(app.currentTheme, 'classic-light');
    assert.equal(context.__state.cssProperties.get('--bg-primary'), '#ffffff');
    assert.ok(context.__state.rootClasses.has('light'));
    assert.equal(context.localStorage.getItem('tvx-theme'), 'classic-light');

    const accent = app.THEMES['classic-light']._accents[1];
    app.setAccent(accent);
    assert.equal(context.__state.cssProperties.get('--accent-color'), accent.color);
    app.setFont('inter', '"Inter", sans-serif');
    assert.equal(context.__state.appendedLinks.length, 1);
    app.setFontSize('large', '18px');
    app.setTweetDensity('compact', '8px');
    assert.equal(context.__state.cssProperties.get('--font-size-base'), '18px');
    assert.equal(context.__state.cssProperties.get('--tweet-padding'), '8px');

    app.panelStack = [{ type: 'thread' }];
    app.panelMode = 'thread';
    app.toggleSplitPanel(false);
    assert.deepEqual(Array.from(app.panelStack), []);
    assert.equal(app.panelMode, null);
});

test('thread and quote navigation update history, panel state, and network state', async () => {
    const context = browserContext();
    context.fetch = async url => ({
        ok: true,
        async json() {
            if (url.includes('/quotes')) {
                return { tweets: [{ tweet_id: 'q1' }], total: 21, limit: 20 };
            }
            return { main: { tweet_id: '42' }, parents: [], children: [] };
        },
    });
    const { tweetApp } = loadScripts(
        context,
        ['themes.js', 'app.js'],
        '({tweetApp})',
    );
    const app = immediateComponent(tweetApp());
    app.$refs.detailPanel = { scrollTop: 99 };

    await app.openThread('42');
    assert.equal(app.viewMode, 'thread');
    assert.equal(app.threadData.main.tweet_id, '42');
    assert.equal(app.loadingThread, false);
    assert.ok(
        context.__state.historyCalls.some(
            call => call[0] === 'push' && call[1].tweetId === '42',
        ),
    );

    await app.openQuotes('42');
    assert.equal(app.viewMode, 'quotes');
    assert.deepEqual(Array.from(app.quotesList, tweet => tweet.tweet_id), ['q1']);
    assert.equal(app.quotesTotalPages, 2);

    app.splitPanel = true;
    context.window.innerWidth = 1280;
    await app.openThread('42');
    assert.equal(app.panelMode, 'thread');
    assert.equal(app.panelThreadData.main.tweet_id, '42');
    await app.openQuotes('42', true);
    assert.equal(app.panelMode, 'quotes');
    assert.equal(app.panelStack.length, 2);
    app.panelGoBack();
    assert.equal(app.panelMode, 'thread');
    app.closePanel();
    assert.equal(app.panelMode, null);
});

test('goBack tears down playing videos before navigating history', () => {
    const context = browserContext();
    const video = {
        paused: false,
        currentTime: 30,
        pause() {
            this.paused = true;
        },
    };
    context.__state.videos.push(video);
    const { tweetApp } = loadScripts(
        context,
        ['themes.js', 'app.js'],
        '({tweetApp})',
    );
    const app = immediateComponent(tweetApp());
    app.goBack();
    assert.equal(video.paused, true);
    assert.equal(video.currentTime, 0);
    assert.equal(context.__state.historyCalls.at(-1)[0], 'back');
});

test('tag editing deduplicates case-insensitively and persists response state', async () => {
    const context = browserContext();
    const requests = [];
    context.fetch = async (url, options = {}) => {
        requests.push([url, options]);
        return {
            ok: true,
            async json() {
                return { tags: [{ tag: 'Night', count: 2 }] };
            },
        };
    };
    const { tweetApp } = loadScripts(
        context,
        ['themes.js', 'app.js'],
        '({tweetApp})',
    );
    const app = immediateComponent(tweetApp());
    app.tweets = [{ tweet_id: '1', media_tags: { tags: ['Old'] } }];
    app.tagModalTweetId = '1';
    app.tagModalData = app.tweets[0].media_tags;
    app.editableTags = ['Night'];
    app.addEditableTag(' night ');
    app.addEditableTag('Sky');
    assert.deepEqual(Array.from(app.editableTags), ['Night', 'Sky']);

    await app.saveTags();
    assert.deepEqual(Array.from(app.tweets[0].media_tags.tags), ['Night', 'Sky']);
    assert.equal(requests[0][1].method, 'PUT');
    assert.deepEqual(JSON.parse(requests[0][1].body), { tags: ['Night', 'Sky'] });

    await app.fetchGlobalTags();
    assert.equal(app.globalTags[0].tag, 'Night');
    assert.equal(app.globalTagsLoading, false);
});

test('config and stats requests update their matching UI state', async () => {
    const context = browserContext();
    context.fetch = async url => ({
        ok: true,
        async json() {
            if (url === '/api/config/schema') {
                return {
                    whitelist: ['web.host'],
                    blacklist: ['auth.auth_token'],
                    full_width: ['web.host'],
                    types: { 'web.port': 'number' },
                };
            }
            if (url === '/api/config') {
                return {
                    auth: { auth_token: 'masked' },
                    sync: { page_delay: 2 },
                    web: { host: '127.0.0.1', port: 8000 },
                };
            }
            if (url === '/api/stats/summary') return { latest_sync: '2026-07-30T00:00:00Z' };
            return {};
        },
    });
    const { tweetApp } = loadScripts(
        context,
        ['themes.js', 'app.js'],
        '({tweetApp})',
    );
    const app = immediateComponent(tweetApp());

    await app.fetchConfig();
    assert.equal(app.configData.web.port, 8000);
    assert.equal(app.isFieldVisible('web', 'host'), true);
    assert.equal(app.isFieldVisible('auth', 'auth_token'), false);
    assert.equal(app.isFieldVisible('sync', 'page_delay'), false);
    app.showAdvancedConfig = true;
    assert.equal(app.isFieldVisible('sync', 'page_delay'), true);
    assert.equal(app.isFieldVisible('auth', 'auth_token'), false);
    assert.equal(app.getFieldType('web', 'port'), 'number');
    assert.equal(app.isFieldFullWidth('web', 'host'), true);

    await app.fetchStats();
    assert.equal(app.lastSyncFormatted, '2026-07-30T00:00:00Z');
});

test('text and card renderers escape HTML and reject active URL schemes', () => {
    const context = browserContext();
    const { tweetApp } = loadScripts(
        context,
        ['themes.js', 'app.js'],
        '({tweetApp})',
    );
    const app = immediateComponent(tweetApp());
    const formatted = app.formatText({
        tweet_id: '1',
        text: '<img src=x onerror=alert(1)> @alice #topic https://t.co/x',
        raw_json: {
            legacy: {
                entities: {
                    urls: [
                        {
                            url: 'https://t.co/x',
                            expanded_url: 'javascript:alert(1)',
                            display_url: '<svg onload=alert(1)>',
                        },
                    ],
                },
            },
        },
    });
    assert.match(formatted, /&lt;img/);
    assert.match(formatted, /class="mention/);
    assert.match(formatted, /class="hashtag/);
    assert.match(formatted, /href="#"/);
    assert.match(formatted, /&lt;svg/);
    assert.doesNotMatch(formatted, /<img src=x/);

    const card = app.renderCard({
        raw_json: {
            card: {
                name: 'summary_large_image',
                binding_values: [
                    { key: 'title', value: { string_value: '<img onerror=x>' } },
                    { key: 'description', value: { string_value: '<script>x</script>' } },
                    { key: 'card_url', value: { string_value: 'javascript:x' } },
                    {
                        key: 'thumbnail_image_original',
                        value: { image_value: { url: 'data:text/html,x' } },
                    },
                ],
            },
        },
    });
    assert.match(card, /href="#"/);
    assert.match(card, /src="#"/);
    assert.match(card, /&lt;img onerror=x&gt;/);
    assert.doesNotMatch(card, /<script>/);
});

test('media renderer preserves dimensions and selects photo, video, GIF, and placeholders', () => {
    const context = browserContext();
    const { tweetApp } = loadScripts(
        context,
        ['themes.js', 'app.js'],
        '({tweetApp})',
    );
    const app = immediateComponent(tweetApp());

    const photo = app.renderMediaGrid([
        {
            type: 'photo',
            width: 1200,
            height: 600,
            download: { local_path: 'media/a.jpg' },
        },
    ]);
    assert.match(photo, /aspect-ratio: 2/);
    assert.match(photo, /<img src="\/media\/a.jpg"/);

    const video = app.renderMediaGrid([
        {
            type: 'video',
            duration_millis: 120000,
            download: {
                local_path: 'media/a.mp4',
                thumbnail_local_path: 'media/a.jpg',
            },
        },
    ]);
    assert.match(video, /controls/);
    assert.doesNotMatch(video, /autoplay muted/);

    const gif = app.renderMediaGrid([
        {
            type: 'animated_gif',
            download: { local_path: 'media/a.mp4' },
        },
    ]);
    assert.match(gif, /autoplay muted playsinline/);
    assert.match(gif, /loop/);

    const missing = app.renderMediaGrid([{ type: 'photo', width: 4, height: 3 }]);
    assert.match(missing, /Media not downloaded/);

    const grid = app.renderMediaGrid([
        { type: 'photo', download: { local_path: 'media/1.jpg' } },
        { type: 'photo', download: { local_path: 'media/2.jpg' } },
        { type: 'photo', download: { local_path: 'media/3.jpg' } },
    ]);
    assert.match(grid, /grid-rows-2/);
    assert.match(grid, /row-span-2/);
});

test('community notes escape headings, text, labels, and external links', () => {
    const context = browserContext();
    const { tweetApp } = loadScripts(
        context,
        ['themes.js', 'app.js'],
        '({tweetApp})',
    );
    const app = immediateComponent(tweetApp());
    const note = app.renderCommunityNote({
        birdwatch_pivot: {
            shorttitle: '<img onerror=x>',
            subtitle: {
                text: 'Read source',
                entities: [
                    {
                        fromIndex: 5,
                        toIndex: 11,
                        ref: { urlType: 'ExternalUrl', url: '" onmouseover="x' },
                    },
                ],
            },
        },
    });
    assert.match(note, /&lt;img onerror=x&gt;/);
    assert.match(note, /href="&quot; onmouseover=&quot;x"/);
    assert.doesNotMatch(note, /<img onerror/);
});

async function main() {
    let failures = 0;
    for (const { name, fn } of tests) {
        try {
            await fn();
            process.stdout.write(`ok - ${name}\n`);
        } catch (error) {
            failures += 1;
            process.stderr.write(`not ok - ${name}\n${error.stack}\n`);
        }
    }
    process.stdout.write(`\n${tests.length - failures}/${tests.length} browser asset tests passed\n`);
    if (failures) process.exitCode = 1;
}

main();
