/**
 * Main Alpine.js application for tweetxvault Web UI.
 */

function tweetApp() {
    return {
        viewMode: 'list',
        tweets: [],
        loading: true,
        loadingMore: false,
        page: 1,
        totalPages: 1,
        total: 0,
        collectionFilter: 'all',
        sortOrder: 'default',
        searchQuery: '',
        error: null,
        showScrollTop: false,
        
        threadData: null,
        loadingThread: false,
        threadCache: new Map(),
        threadCacheTtlMs: 60000,
        
        quotesTweetId: null,
        quotesList: [],
        quotesPage: 1,
        quotesTotalPages: 1,
        quotesLoading: false,
        quotesLoadingMore: false,
        
        lightboxOpen: false,
        lightboxMedia: [],
        lightboxIndex: 0,
        videoObserver: null,

        // Split panel state
        splitPanel: false,
        panelStack: [],
        panelMode: null,        // 'thread' | 'quotes' | null
        panelThreadData: null,
        panelLoadingThread: false,
        panelQuotesTweetId: null,
        panelQuotesList: [],
        panelQuotesPage: 1,
        panelQuotesTotalPages: 1,
        panelQuotesLoading: false,
        panelQuotesLoadingMore: false,

        tweetMenuOpen: null,
        tweetMenuPressTimer: null,
        tweetMenuClickBlockedUntil: 0,

        tagModalOpen: false,
        tagModalData: null,
        tagModalTweetId: null,
        
        isEditingTags: false,
        editableTags: [],
        editableDescription: '',
        tagSearchQuery: '',
        tagAutocompleteOptions: [],
        tagSearchDropdown: false,
        
        showKeyboardShortcuts: false,
        profileCard: null,

        openProfileCard(event, tweet, isQt = false) {
            let anchor = event.currentTarget;
            if (!anchor.classList.contains('w-10')) {
                // Attempt to find the pfp avatar container which is usually a previous sibling or in the parent flex row
                let container = anchor.closest('.flex.space-x-2') || anchor.closest('.flex.relative');
                if (!container && anchor.parentElement && anchor.parentElement.parentElement) {
                    container = anchor.parentElement.parentElement;
                }
                if (container) {
                    const pfp = container.querySelector('.w-10.h-10.rounded-full');
                    if (pfp) anchor = pfp;
                }
            }

            const rect = anchor.getBoundingClientRect();
            let x = rect.left;
            let y = rect.bottom + 8;
            
            let name, username, id;
            let raw;
            if (isQt) {
                raw = tweet.raw_json || tweet;
                const authorInfo = this.getQuoteAuthor(tweet);
                name = authorInfo.name;
                username = authorInfo.screen_name;
                id = authorInfo.id;
            } else {
                raw = tweet.raw_json || {};
                name = tweet.author?.display_name || 'Unknown';
                username = tweet.author?.username || 'unknown';
                id = tweet.author?.id || 'unknown';
            }
            
            const core = raw.core || {};
            const userResult = core.user_results?.result || {};
            const legacy = userResult.legacy || raw.user || {};
            
            this.profileCard = {
                x,
                y,
                name,
                username,
                id,
                initial: name.charAt(0).toUpperCase(),
                description: legacy.description || '',
                followersCount: legacy.followers_count || 0,
                followingCount: legacy.friends_count || 0,
                syncDate: tweet.synced_at || (tweet.collection ? tweet.collection.synced_at : null) || raw.synced_at || null
            };
            
            this.$nextTick(() => {
                const card = document.getElementById('profile-card-modal');
                if (card) {
                    const cardRect = card.getBoundingClientRect();
                    if (this.profileCard.x + cardRect.width > window.innerWidth) {
                        this.profileCard.x = window.innerWidth - cardRect.width - 16;
                    }
                    if (this.profileCard.y + cardRect.height > window.innerHeight) {
                        this.profileCard.y = rect.top - cardRect.height - 8;
                    }
                }
            });

            if (!this._profileMouseMoveHandler) {
                this._profileMouseMoveHandler = (e) => {
                    if (!this.profileCard) return;
                    const card = document.getElementById('profile-card-modal');
                    if (!card) return;
                    const cardRect = card.getBoundingClientRect();
                    const bufferX = 30;
                    const bufferY = 80;
                    if (
                        e.clientX < cardRect.left - bufferX ||
                        e.clientX > cardRect.right + bufferX ||
                        e.clientY < cardRect.top - bufferY ||
                        e.clientY > cardRect.bottom + bufferY
                    ) {
                        this.closeProfileCard();
                    }
                };
            }
            window.addEventListener('mousemove', this._profileMouseMoveHandler);
        },
        
        closeProfileCard() {
            this.profileCard = null;
            if (this._profileMouseMoveHandler) {
                window.removeEventListener('mousemove', this._profileMouseMoveHandler);
            }
        },
        
        showSettingsModal: false,
        settingsTab: 'tags',
        globalTags: [],
        globalTagsLoading: false,
        tagSearchTerm: '',
        
        configSchema: null,
        configData: null,
        configSaving: false,
        showAdvancedConfig: false,
        
        mergePrimaryTag: '',
        mergeTagsList: [],
        mergeSearchTerm: '',
        mergePrimarySearchTerm: '',
        mergePrimaryDropdown: false,
        mergeTagsDropdown: false,
        mergeSelectedIndex: 0,
        primarySelectedIndex: 0,
        
        lastSyncAt: null,
        lastSyncFormatted: '',
        darkMode: true,
        
        showStatsModal: false,
        statsSummary: null,
        loadingStatsSummary: false,
        statsCollections: [],
        loadingStatsCollections: false,
        statsHealth: null,
        loadingStatsHealth: false,
        showEmptyUnavailableReasons: false,
        hoveredUnavailableReason: null,
        archiveEnrichmentIncomplete: 0,
        statsTags: null,
        loadingStatsTags: false,
        loadingStatsSnapshot: false,
        statsGeneratedAt: null,
        statsRefreshing: false,
        statsRefreshFailed: false,
        statsRefreshPollTimer: null,
        statsRefreshPollAttempts: 0,
        statsAgeNow: Date.now(),

        storageData: null,
        storageLoading: false,
        storageDetailedView: false,
        storageGapsCollapsed: false,
        animatingStorageToggle: false,
        hoveredStorageId: null,
        expandedStorageId: null,
        
        THEMES: THEMES,
        currentTheme: 'classic-dark',
        currentAccent: null,

        fontFamily: 'system',
        fontSize: 'default',
        tweetDensity: 'default',

        FONT_OPTIONS: [
            {key: 'system', label: 'System Default', family: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'},
            {key: 'inter', label: 'Inter', family: '"Inter", sans-serif'},
            {key: 'roboto', label: 'Roboto', family: '"Roboto", sans-serif'},
            {key: 'jakarta', label: 'Plus Jakarta Sans', family: '"Plus Jakarta Sans", sans-serif'},
            {key: 'outfit', label: 'Outfit', family: '"Outfit", sans-serif'},
            {key: 'nunito', label: 'Nunito', family: '"Nunito", sans-serif'},
            {key: 'jetbrains', label: 'JetBrains Mono', family: '"JetBrains Mono", monospace'}
        ],

        initApp() {
            if ('scrollRestoration' in history) {
                history.scrollRestoration = 'manual';
            }

            const savedTheme = localStorage.getItem('tvx-theme') || localStorage.getItem('theme');
            if (savedTheme && THEMES[savedTheme]) {
                this.applyTheme(savedTheme);
            } else if (savedTheme === 'light') {
                this.applyTheme('classic-light');
            }
            
            const savedFont = localStorage.getItem('tvx-font');
            const savedFontFamily = localStorage.getItem('tvx-font-family');
            if (savedFont && savedFontFamily) {
                this.fontFamily = savedFont;
                this.loadFont(savedFont);
                document.body.style.fontFamily = savedFontFamily;
            }

            window.addEventListener('scroll', () => {
                this.showScrollTop = window.scrollY > 300;
            });
            
            const savedFontSize = localStorage.getItem('tvx-font-size');
            const savedFontSizePx = localStorage.getItem('tvx-font-size-px');
            if (savedFontSize && savedFontSizePx) {
                this.fontSize = savedFontSize;
                document.documentElement.style.setProperty('--font-size-base', savedFontSizePx);
            }
            
            const savedDensity = localStorage.getItem('tvx-density');
            const savedDensityPx = localStorage.getItem('tvx-density-px');
            if (savedDensity && savedDensityPx) {
                this.tweetDensity = savedDensity;
                document.documentElement.style.setProperty('--tweet-padding', savedDensityPx);
            }
            
            // Restore split panel preference
            this.splitPanel = localStorage.getItem('tvx-split-panel') === 'true';
            
            
            this.fetchStats();
            this.fetchArchiveEnrichmentStatus();
            setInterval(() => this.fetchArchiveEnrichmentStatus(), 60000);
            setInterval(() => { this.statsAgeNow = Date.now(); }, 30000);
            this.fetchGlobalTags();
            
            this.$watch('showSettingsModal', val => {
                if (val) {
                    this.fetchConfig();
                }
            });
            
            window.addEventListener('open-lightbox', (e) => {
                this.lightboxMedia = e.detail.media;
                this.lightboxIndex = e.detail.index;
                this.lightboxOpen = true;
            });
            
            this.videoObserver = new IntersectionObserver((entries) => {
                entries.forEach(e => {
                    if (e.target.tagName === 'VIDEO') {
                        if (!e.isIntersecting) {
                            e.target.pause();
                        } else if (e.target.hasAttribute('autoplay')) {
                            e.target.play().catch(() => {});
                        }
                    }
                });
            }, { threshold: 0.1 });

            const domObserver = new MutationObserver((mutations) => {
                mutations.forEach(m => {
                    m.addedNodes.forEach(node => {
                        if (node.nodeType === 1) {
                            if (node.tagName === 'VIDEO') this.videoObserver.observe(node);
                            node.querySelectorAll('video').forEach(v => this.videoObserver.observe(v));
                        }
                    });
                });
            });
            domObserver.observe(document.body, { childList: true, subtree: true });
            
            history.replaceState({ viewMode: 'list', scrollY: window.scrollY }, '');
            
            window.addEventListener('popstate', (e) => {
                this.pauseAllVideos();
                if (e.state && e.state.viewMode === 'thread') {
                    this.openThread(e.state.tweetId, true);
                } else if (e.state && e.state.viewMode === 'quotes') {
                    this.viewMode = 'quotes';
                    this.quotesTweetId = e.state.quotesTweetId;
                    if (this.quotesList.length === 0) {
                        this.fetchQuotes();
                    }
                } else {
                    this.viewMode = 'list';
                    if (e.state && e.state.scrollY !== undefined) {
                        window.scrollTo(0, e.state.scrollY);
                        this.$nextTick(() => {
                            window.scrollTo(0, e.state.scrollY);
                            setTimeout(() => window.scrollTo(0, e.state.scrollY), 20);
                        });
                    }
                }
            });

            this.fetchTweets();
        },

        pauseAllVideos() {
            document.querySelectorAll('video').forEach(v => {
                v.pause();
                v.currentTime = 0;
            });
        },

        scrollToTop() {
            window.scrollTo({ top: 0, behavior: 'smooth' });
        },

        async fetchTweets(append = false) {
            if (!append) {
                this.loading = true;
                this.tweets = [];
                window.scrollTo(0, 0);
            } else {
                this.loadingMore = true;
            }
            
            let url = `/api/tweets?collection=${this.collectionFilter}&sort=${this.sortOrder}&page=${this.page}`;
            if (this.searchQuery.trim()) {
                url += `&q=${encodeURIComponent(this.searchQuery)}`;
            }

            try {
                this.error = null;
                const res = await fetch(url);
                
                let data;
                try {
                    data = await res.json();
                } catch (parseError) {
                    throw new Error(`Invalid server response (${res.status}): Please check server logs.`);
                }
                
                if (!res.ok) {
                    throw new Error(data.detail || `Server error: ${res.status}`);
                }
                
                if (append) {
                    this.tweets = [...this.tweets, ...data.tweets];
                } else {
                    this.tweets = data.tweets;
                }
                this.page = data.page;
                this.totalPages = data.pages;
                this.total = data.total || 0;
            } catch (e) {
                console.error(e);
                this.error = e.message;
            } finally {
                this.loading = false;
                this.loadingMore = false;
            }
        },
        
        toggleTheme() {
            // Legacy — handled by applyTheme
        },

        applyTheme(key) {
            const theme = THEMES[key];
            if (!theme) return;
            this.currentTheme = key;
            const root = document.documentElement;
            for (const [prop, val] of Object.entries(theme)) {
                if (prop.startsWith('--')) root.style.setProperty(prop, val);
            }
            const bgHex = theme['--bg-primary'] || '#000000';
            const r = parseInt(bgHex.slice(1,3),16), g = parseInt(bgHex.slice(3,5),16), b = parseInt(bgHex.slice(5,7),16);
            this.darkMode = (r*0.299 + g*0.587 + b*0.114) < 128;
            if (this.darkMode) { root.classList.remove('light'); } else { root.classList.add('light'); }

            // Restore saved accent for this theme (if it has selectable accents)
            if (theme._accents) {
                const saved = localStorage.getItem('tvx-accent-' + key);
                const accent = saved ? theme._accents.find(a => a.color === saved) : null;
                if (accent) {
                    this.currentAccent = accent.color;
                    root.style.setProperty('--accent-color', accent.color);
                    root.style.setProperty('--accent-hover', accent.hover);
                    root.style.setProperty('--accent-text', accent.text);
                } else {
                    this.currentAccent = theme['--accent-color'];
                }
            } else {
                this.currentAccent = theme['--accent-color'];
            }

            localStorage.setItem('tvx-theme', key);
        },

        setAccent(accent) {
            this.currentAccent = accent.color;
            const root = document.documentElement;
            root.style.setProperty('--accent-color', accent.color);
            root.style.setProperty('--accent-hover', accent.hover);
            root.style.setProperty('--accent-text', accent.text);
            localStorage.setItem('tvx-accent-' + this.currentTheme, accent.color);
        },

        loadFont(key) {
            if (key === 'system' || document.querySelector(`link[data-font="${key}"]`)) return;
            const url = FONT_URLS[key];
            if (!url) return;
            const link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = url;
            link.dataset.font = key;
            document.head.appendChild(link);
        },

        setFont(key, family) {
            this.fontFamily = key;
            this.loadFont(key);
            document.body.style.fontFamily = family;
            localStorage.setItem('tvx-font', key);
            localStorage.setItem('tvx-font-family', family);
        },

        setFontSize(key, px) {
            this.fontSize = key;
            document.documentElement.style.setProperty('--font-size-base', px);
            localStorage.setItem('tvx-font-size', key);
            localStorage.setItem('tvx-font-size-px', px);
        },

        setTweetDensity(key, px) {
            this.tweetDensity = key;
            document.documentElement.style.setProperty('--tweet-padding', px);
            localStorage.setItem('tvx-density', key);
            localStorage.setItem('tvx-density-px', px);
        },
        
        toggleSplitPanel(val) {
            this.splitPanel = val;
            localStorage.setItem('tvx-split-panel', val);
            if (!val) this.closePanel();
        },

        closePanel() {
            this.panelStack = [];
            this.panelMode = null;
            this.panelThreadData = null;
            this.panelQuotesList = [];
        },

        panelGoBack() {
            if (this.panelStack.length <= 1) return;
            this.panelStack.pop();
            const prev = this.panelStack[this.panelStack.length - 1];
            this.panelMode = prev.type;
            if (prev.type === 'thread') {
                this.panelThreadData = prev.data;
                this.panelLoadingThread = false;
            } else if (prev.type === 'quotes') {
                this.panelQuotesList = prev.data;
                this.panelQuotesLoading = false;
            }
            this.$nextTick(() => {
                if (this.$refs.detailPanel) this.$refs.detailPanel.scrollTop = prev.scrollY || 0;
            });
        },

        async fetchPanelQuotes(append = false) {
            if (append) this.panelQuotesLoadingMore = true;
            else this.panelQuotesLoading = true;
            try {
                const res = await fetch(`/api/tweets/${this.panelQuotesTweetId}/quotes?page=${this.panelQuotesPage}`);
                const data = await res.json();
                if (append) this.panelQuotesList = [...this.panelQuotesList, ...data.tweets];
                else this.panelQuotesList = data.tweets;
                this.panelQuotesTotalPages = Math.ceil(data.total / data.limit);
                // Cache on stack
                const top = this.panelStack[this.panelStack.length - 1];
                if (top) top.data = this.panelQuotesList;
            } catch (e) {
                console.error('Failed to fetch panel quotes', e);
            } finally {
                this.panelQuotesLoading = false;
                this.panelQuotesLoadingMore = false;
            }
        },
        
        loadMore() {
            if (this.loadingMore || this.loading || this.page >= this.totalPages) return;
            this.page++;
            this.fetchTweets(true);
        },

        async loadThread(tweetId) {
            const cached = this.threadCache.get(tweetId);
            if (cached && Date.now() - cached.loadedAt < this.threadCacheTtlMs) {
                return cached.request;
            }

            const request = (async () => {
                const res = await fetch(`/api/tweets/${tweetId}`);
                if (!res.ok) throw new Error('Failed to load thread');
                return res.json();
            })();
            const cacheEntry = { request, loadedAt: Date.now() };
            this.threadCache.set(tweetId, cacheEntry);
            if (this.threadCache.size > 25) {
                this.threadCache.delete(this.threadCache.keys().next().value);
            }
            try {
                return await request;
            } catch (error) {
                if (this.threadCache.get(tweetId) === cacheEntry) {
                    this.threadCache.delete(tweetId);
                }
                throw error;
            }
        },

        async openThread(tweetId, fromPopState = false, fromPanel = false) {
            // Split panel mode (desktop only)
            if (this.splitPanel && window.innerWidth >= 1024 && !fromPopState) {
                if (!fromPanel) {
                    // Clicked from list - reset panel stack
                    this.panelStack = [{ type: 'thread', tweetId }];
                } else {
                    // Clicked from within panel - save current state and push
                    if (this.panelStack.length > 0) {
                        const top = this.panelStack[this.panelStack.length - 1];
                        top.scrollY = this.$refs.detailPanel?.scrollTop || 0;
                        if (this.panelMode === 'thread') top.data = this.panelThreadData;
                        else if (this.panelMode === 'quotes') top.data = this.panelQuotesList;
                    }
                    this.panelStack.push({ type: 'thread', tweetId });
                }
                this.panelMode = 'thread';
                this.panelLoadingThread = true;
                this.panelThreadData = null;
                this.$nextTick(() => {
                    if (this.$refs.detailPanel) this.$refs.detailPanel.scrollTop = 0;
                });
                try {
                    this.panelThreadData = await this.loadThread(tweetId);
                    const top = this.panelStack[this.panelStack.length - 1];
                    if (top) top.data = this.panelThreadData;
                } catch (e) {
                    console.error(e);
                    if (fromPanel && this.panelStack.length > 1) this.panelStack.pop();
                } finally {
                    this.panelLoadingThread = false;
                }
                return;
            }

            if (this.viewMode === 'list') {
                history.replaceState({ viewMode: 'list', scrollY: window.scrollY }, '');
            } else if (this.viewMode === 'quotes') {
                history.replaceState({ viewMode: 'quotes', quotesTweetId: this.quotesTweetId, scrollY: window.scrollY }, '');
            }
            this.viewMode = 'thread';
            this.loadingThread = true;
            this.threadData = null;
            window.scrollTo(0, 0);

            if (!fromPopState) {
                history.pushState({ viewMode: 'thread', tweetId }, '');
            }

            try {
                this.threadData = await this.loadThread(tweetId);
            } catch (e) {
                console.error(e);
                this.goBack();
            } finally {
                this.loadingThread = false;
            }
        },
        
        async openQuotes(tweetId, fromPanel = false) {
            // Split panel mode
            if (this.splitPanel && window.innerWidth >= 1024) {
                if (this.panelStack.length > 0) {
                    const top = this.panelStack[this.panelStack.length - 1];
                    top.scrollY = this.$refs.detailPanel?.scrollTop || 0;
                    if (this.panelMode === 'thread') top.data = this.panelThreadData;
                    else if (this.panelMode === 'quotes') top.data = this.panelQuotesList;
                }
                this.panelStack.push({ type: 'quotes', tweetId });
                this.panelMode = 'quotes';
                this.panelQuotesTweetId = tweetId;
                this.panelQuotesPage = 1;
                this.panelQuotesList = [];
                this.panelQuotesLoading = true;
                this.$nextTick(() => {
                    if (this.$refs.detailPanel) this.$refs.detailPanel.scrollTop = 0;
                });
                await this.fetchPanelQuotes();
                return;
            }

            history.replaceState({ viewMode: this.viewMode, tweetId: this.threadData?.main?.tweet_id, scrollY: window.scrollY }, '');
            this.viewMode = 'quotes';
            this.quotesTweetId = tweetId;
            this.quotesPage = 1;
            this.quotesList = [];
            this.quotesLoading = true;
            window.scrollTo(0, 0);
            
            history.pushState({ viewMode: 'quotes', quotesTweetId: tweetId }, '');
            
            await this.fetchQuotes();
        },
        
        async fetchQuotes(append = false) {
            if (append) this.quotesLoadingMore = true;
            else this.quotesLoading = true;
            
            try {
                const res = await fetch(`/api/tweets/${this.quotesTweetId}/quotes?page=${this.quotesPage}`);
                const data = await res.json();
                
                if (append) this.quotesList = [...this.quotesList, ...data.tweets];
                else this.quotesList = data.tweets;
                
                this.quotesTotalPages = Math.ceil(data.total / data.limit);
            } catch (e) {
                console.error(e);
            } finally {
                this.quotesLoading = false;
                this.quotesLoadingMore = false;
            }
        },
        
        goBack() {
            // In split panel mode, goBack just closes the panel
            if (this.splitPanel && window.innerWidth >= 1024 && this.panelStack.length > 0) {
                this.closePanel();
                return;
            }
            this.pauseAllVideos();
            history.back();
        },

        async fetchStats() {
            try {
                const res = await fetch('/api/stats/latest-sync');
                const data = await res.json();
                if (data.latest_sync) {
                    this.lastSyncFormatted = data.latest_sync;
                }
            } catch (e) {
                console.error('Failed to fetch stats', e);
            }
        },

        async openStatsModal() {
            this.showStatsModal = true;
            await this.fetchStatsSnapshot();
        },

        setStatsLoading(loading) {
            this.loadingStatsSnapshot = loading;
            this.loadingStatsSummary = loading;
            this.loadingStatsCollections = loading;
            this.loadingStatsHealth = loading;
            this.loadingStatsTags = loading;
            this.storageLoading = loading;
        },

        applyStatsSnapshot(snapshot) {
            this.statsSummary = snapshot.summary;
            this.statsCollections = snapshot.collections || [];
            this.statsHealth = snapshot.health;
            this.storageData = snapshot.storage;
            this.statsTags = snapshot.tags;
            this.statsGeneratedAt = snapshot.generated_at;
            this.statsRefreshing = Boolean(snapshot.refreshing);
            this.statsRefreshFailed = Boolean(snapshot.refresh_failed);
            this.statsAgeNow = Date.now();
            if (snapshot.summary?.latest_sync) {
                this.lastSyncFormatted = snapshot.summary.latest_sync;
            }
        },

        async fetchStatsSnapshot(revalidate = true) {
            const showSkeletons = !this.statsGeneratedAt;
            if (showSkeletons) this.setStatsLoading(true);
            try {
                const res = await fetch(`/api/stats/snapshot?revalidate=${revalidate}`);
                if (!res.ok) throw new Error(`Statistics request failed (${res.status})`);
                this.applyStatsSnapshot(await res.json());
                if (this.statsRefreshing) {
                    this.statsRefreshPollAttempts = 0;
                    this.scheduleStatsRefreshPoll();
                }
            } catch (e) {
                console.error('Failed to fetch statistics', e);
                this.statsRefreshFailed = true;
            } finally {
                if (showSkeletons) this.setStatsLoading(false);
            }
        },

        async refreshStats() {
            if (this.statsRefreshing || this.loadingStatsSnapshot) return;
            this.statsRefreshing = true;
            this.statsRefreshFailed = false;
            try {
                const res = await fetch('/api/stats/refresh', { method: 'POST' });
                if (!res.ok) throw new Error(`Statistics refresh failed (${res.status})`);
                this.applyStatsSnapshot(await res.json());
                if (this.statsRefreshing) {
                    this.statsRefreshPollAttempts = 0;
                    this.scheduleStatsRefreshPoll();
                }
            } catch (e) {
                console.error('Failed to refresh statistics', e);
                this.statsRefreshing = false;
                this.statsRefreshFailed = true;
            }
        },

        scheduleStatsRefreshPoll() {
            if (this.statsRefreshPollTimer) clearTimeout(this.statsRefreshPollTimer);
            if (!this.statsRefreshing || !this.showStatsModal) return;
            this.statsRefreshPollAttempts += 1;
            if (this.statsRefreshPollAttempts > 300) {
                this.statsRefreshing = false;
                this.statsRefreshFailed = true;
                return;
            }
            this.statsRefreshPollTimer = setTimeout(() => this.pollStatsRefresh(), 1000);
        },

        async pollStatsRefresh() {
            this.statsRefreshPollTimer = null;
            if (!this.showStatsModal) return;
            try {
                const res = await fetch('/api/stats/snapshot?revalidate=false');
                if (!res.ok) throw new Error(`Statistics refresh poll failed (${res.status})`);
                this.applyStatsSnapshot(await res.json());
            } catch (e) {
                console.error('Failed to check statistics refresh', e);
            }
            if (this.statsRefreshing) {
                this.scheduleStatsRefreshPoll();
            } else {
                this.statsRefreshPollAttempts = 0;
            }
        },

        statsAgeLabel() {
            if (!this.statsGeneratedAt) return '';
            // Reading the timer-backed value keeps Alpine's relative label current.
            void this.statsAgeNow;
            const relative = this.formatRelativeDate(this.statsGeneratedAt);
            if (this.statsRefreshFailed) return `Refresh failed · updated ${relative}`;
            if (this.statsRefreshing) return `Refreshing · updated ${relative}`;
            return `Updated ${relative}`;
        },

        getUnavailableReasons() {
            const reasons = this.statsHealth?.enrichment?.unavailable?.reasons || [];
            return this.showEmptyUnavailableReasons
                ? reasons
                : reasons.filter(reason => (reason.count || 0) > 0);
        },

        getUnavailableBarReasons() {
            const reasons = this.statsHealth?.enrichment?.unavailable?.reasons || [];
            return reasons.filter(reason => (reason.count || 0) > 0);
        },

        getUnavailableSegmentWidth(reason) {
            const reasons = this.getUnavailableBarReasons();
            if (!reasons.length) return 0;

            const minFloor = 1.2;
            const logWeights = reasons.map(item => Math.log10(Math.max(10, item.count || 0)));
            const totalLog = logWeights.reduce((total, weight) => total + weight, 0);
            const rawWeights = reasons.map((item, index) => {
                const linearPct = item.percent_of_missing || 0;
                const logPct = totalLog > 0 ? (logWeights[index] / totalLog) * 100 : 0;
                return Math.max(minFloor, (linearPct * 0.82) + (logPct * 0.18));
            });
            const totalWeight = rawWeights.reduce((total, weight) => total + weight, 0);
            const index = reasons.findIndex(item => item.reason === reason.reason);
            return index === -1 ? 0 : Math.max(minFloor, (rawWeights[index] / totalWeight) * 100);
        },

        getUnavailableReasonStatus(reason) {
            const parts = [];
            if ((reason.due || 0) > 0) parts.push(`${reason.due.toLocaleString()} due now`);
            if ((reason.delayed || 0) > 0) parts.push(`${reason.delayed.toLocaleString()} scheduled`);
            if ((reason.permanent || 0) > 0) parts.push(`${reason.permanent.toLocaleString()} permanent`);
            if (parts.length) return parts.join(' · ');
            return (reason.count || 0) > 0 ? 'Availability recorded' : 'No unavailable tweets';
        },

        fetchArchiveEnrichmentStatus() {
            fetch('/api/stats/enrichment-incomplete')
                .then(r => r.json())
                .then(d => {
                    this.archiveEnrichmentIncomplete = d.incomplete || 0;
                })
                .catch(e => console.error('Failed to fetch archive enrichment status', e));
        },

        setHoveredStorage(id) {
            this.hoveredStorageId = id;
        },
        clearHoveredStorage() {
            this.hoveredStorageId = null;
        },
        getActiveStorageSegments() {
            if (!this.storageData) return [];
            const raw = this.storageDetailedView ? this.storageData.segments : this.storageData.simplified_segments;
            return (raw || []).filter(s => (s.bytes || 0) > 0);
        },
        getSegmentWidth(seg) {
            const segments = this.getActiveStorageSegments();
            if (!segments || !segments.length) return 2;
            
            const minFloor = 1.2;
            const logWeights = segments.map(s => Math.log10(Math.max(10, s.bytes || 0)));
            const totalLog = logWeights.reduce((a, b) => a + b, 0);
            
            const rawWeights = segments.map((s, idx) => {
                const linearPct = s.percent || 0;
                const logPct = totalLog > 0 ? (logWeights[idx] / totalLog) * 100 : 0;
                return Math.max(minFloor, (linearPct * 0.82) + (logPct * 0.18));
            });

            const totalWeight = rawWeights.reduce((a, b) => a + b, 0);
            const myIndex = segments.findIndex(s => s.id === seg.id);
            if (myIndex === -1) return minFloor;
            
            const normalizedWidth = (rawWeights[myIndex] / totalWeight) * 100;
            return Math.max(minFloor, normalizedWidth);
        },
        toggleStorageDetailView() {
            if (this.animatingStorageToggle) return;
            this.animatingStorageToggle = true;

            // Phase 1: Collapse all segment gaps so the bar becomes solid.
            this.storageGapsCollapsed = true;

            // Phase 2: After the bar becomes solid, flip state and reopen the gaps.
            setTimeout(() => {
                this.storageDetailedView = !this.storageDetailedView;
                this.expandedStorageId = null;

                this.$nextTick(() => {
                    this.storageGapsCollapsed = false;
                    setTimeout(() => {
                        this.animatingStorageToggle = false;
                    }, 600);
                });
            }, 250);
        },
        toggleExpandStorage(id) {
            if (!this.storageDetailedView) return; // Only detailed view allows row expansion
            this.expandedStorageId = this.expandedStorageId === id ? null : id;
        },

        updateSyncTime() {
            if (!this.lastSyncAt) return;
            
            const now = new Date();
            const diffMs = now - this.lastSyncAt;
            const diffMins = Math.floor(diffMs / 60000);
            const diffHours = Math.floor(diffMins / 60);
            const diffDays = Math.floor(diffHours / 24);
            
            let relativeStr = '';
            if (diffMins < 1) relativeStr = 'Just now';
            else if (diffMins < 60) relativeStr = `${diffMins}m ago`;
            else if (diffHours < 24) relativeStr = `${diffHours}h ago`;
            else relativeStr = `${diffDays}d ago`;
            
            const options = { year: 'numeric', month: 'short', day: 'numeric' };
            const dateStr = this.lastSyncAt.toLocaleDateString(undefined, options);
            
            this.lastSyncFormatted = `Last sync: ${dateStr} · ${relativeStr}`;
        },

        search() { 
            if (!this.searchQuery.trim() && this.sortOrder === 'default') this.sortOrder = 'newest';
            this.page = 1; this.viewMode = 'list'; this.fetchTweets(); 
        },
        searchFrom(username) {
            if(!username) return;
            this.searchQuery = `from:${username}`;
            this.search();
        },
        
        getInitial(tweet) {
            if (!tweet || !tweet.author) return '?';
            if (tweet.author.display_name) return tweet.author.display_name.charAt(0);
            if (tweet.author.username) return tweet.author.username.charAt(0);
            return '?';
        },

        toggleTweetMenu(menuKey) {
            if (Date.now() < this.tweetMenuClickBlockedUntil) return;
            this.tweetMenuOpen = this.tweetMenuOpen === menuKey ? null : menuKey;
        },

        closeTweetMenu(menuKey = null) {
            if (!menuKey || this.tweetMenuOpen === menuKey) this.tweetMenuOpen = null;
        },

        startTweetMenuPress(event, tweetId, tagsData) {
            if (event?.pointerType === 'mouse' && event.button !== 0) return;
            this.cancelTweetMenuPress();
            this.tweetMenuPressTimer = setTimeout(() => {
                this.tweetMenuPressTimer = null;
                this.tweetMenuClickBlockedUntil = Date.now() + 750;
                this.openTagModal(tweetId, tagsData);
            }, 1000);
        },

        cancelTweetMenuPress() {
            if (this.tweetMenuPressTimer !== null) {
                clearTimeout(this.tweetMenuPressTimer);
                this.tweetMenuPressTimer = null;
            }
        },

        openTagsFromTweetMenu(tweetId, tagsData) {
            this.closeTweetMenu();
            this.openTagModal(tweetId, tagsData);
        },

        openTagModal(tweetId, tagsData) {
            const normalizedData = {
                description: typeof tagsData?.description === 'string' ? tagsData.description : '',
                tags: Array.isArray(tagsData?.tags) ? [...tagsData.tags] : [],
            };
            if (this._tagModalCloseTimer) clearTimeout(this._tagModalCloseTimer);
            this.closeTweetMenu();
            this.tagModalTweetId = tweetId;
            this.tagModalData = normalizedData;
            this.isEditingTags = false;
            this.editableTags = [...normalizedData.tags];
            this.editableDescription = normalizedData.description;
            this.tagModalOpen = true;
            document.body.style.overflow = 'hidden';
        },
        
        closeTagModal() {
            this.tagModalOpen = false;
            this._tagModalCloseTimer = setTimeout(() => {
                this.tagModalData = null;
                this.tagModalTweetId = null;
                this.isEditingTags = false;
                this.editableTags = [];
                this.editableDescription = '';
                this.tagSearchDropdown = false;
                document.body.style.overflow = '';
                this._tagModalCloseTimer = null;
            }, 300);
        },

        updateTweetTagData(tweetId, tagData) {
            const assign = tweet => {
                if (!tweet || tweet.tweet_id !== tweetId) return;
                tweet.media_tags = tagData ? {
                    description: tagData.description,
                    tags: [...tagData.tags],
                } : null;
            };
            const visitThread = thread => {
                if (!thread) return;
                assign(thread.main);
                for (const tweet of thread.parents || []) assign(tweet);
                for (const tweet of thread.children || []) {
                    assign(tweet);
                    for (const reply of tweet.op_replies || []) assign(reply);
                }
            };

            for (const tweet of this.tweets) assign(tweet);
            for (const tweet of this.quotesList) assign(tweet);
            for (const tweet of this.panelQuotesList) assign(tweet);
            visitThread(this.threadData);
            visitThread(this.panelThreadData);
            for (const entry of this.panelStack) {
                if (entry.type === 'thread') visitThread(entry.data);
                else if (entry.type === 'quotes') {
                    for (const tweet of entry.data || []) assign(tweet);
                }
            }
        },
        
        async deleteTags() {
            if (!this.tagModalTweetId || !confirm("Are you sure you want to delete the tags for this tweet?")) return;
            try {
                const res = await fetch(`/api/tags/${this.tagModalTweetId}`, { method: 'DELETE' });
                if (res.ok) {
                    this.updateTweetTagData(this.tagModalTweetId, null);
                    this.closeTagModal();
                } else {
                    alert("Failed to delete tags.");
                }
            } catch (e) {
                alert("Error: " + e.message);
            }
        },
        
        startEditingTags() {
            this.isEditingTags = true;
            this.editableTags = [...(this.tagModalData?.tags || [])];
            this.editableDescription = this.tagModalData?.description || '';
        },
        async fetchTagAutocomplete(query) {
            try {
                const res = await fetch(`/api/tags/autocomplete?q=${encodeURIComponent(query)}`);
                if(res.ok) {
                    const data = await res.json();
                    this.tagAutocompleteOptions = data.tags;
                }
            } catch(e) {}
        },
        addEditableTag(tag) {
            const trimmed = tag.trim();
            if(trimmed && !this.editableTags.find(t => t.toLowerCase() === trimmed.toLowerCase())) {
                this.editableTags.push(trimmed);
            }
            this.tagSearchQuery = '';
        },
        async saveTags() {
            try {
                const description = this.editableDescription.trim();
                const res = await fetch(`/api/tags/${this.tagModalTweetId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ tags: this.editableTags, description })
                });
                if (res.ok) {
                    const savedData = this.editableTags.length || description ? {
                        description,
                        tags: [...this.editableTags],
                    } : null;
                    this.updateTweetTagData(this.tagModalTweetId, savedData);
                    if (savedData) {
                        this.tagModalData = savedData;
                        this.isEditingTags = false;
                    } else {
                        this.closeTagModal();
                    }
                } else {
                    alert("Failed to save tags.");
                }
            } catch(e) {
                alert("Error saving tags: " + e.message);
            }
        },
        
        async fetchGlobalTags() {
            this.globalTagsLoading = true;
            try {
                const res = await fetch('/api/tags/stats');
                if (res.ok) {
                    const data = await res.json();
                    this.globalTags = data.tags;
                }
            } catch(e) { console.error(e); }
            finally { this.globalTagsLoading = false; }
        },
        
        get filteredGlobalTags() {
            if(!this.tagSearchTerm) return this.globalTags;
            const term = this.tagSearchTerm.toLowerCase();
            return this.globalTags.filter(t => t.tag.toLowerCase().includes(term));
        },
        
        async deleteGlobalTag(tag) {
            if(!confirm(`Are you sure you want to permanently delete the tag "${tag}" from all posts?`)) return;
            try {
                const res = await fetch(`/api/tags/global/${encodeURIComponent(tag)}`, { method: 'DELETE' });
                if(res.ok) {
                    this.globalTags = this.globalTags.filter(t => t.tag !== tag);
                } else {
                    alert("Failed to delete global tag");
                }
            } catch(e) {
                alert("Error: " + e.message);
            }
        },
        
        get filteredMergeOptions() {
            let opts = this.globalTags.filter(t => !this.mergeTagsList.includes(t.tag) && t.tag !== this.mergePrimaryTag);
            if(this.mergeSearchTerm) {
                const q = this.mergeSearchTerm.toLowerCase();
                opts = opts.filter(t => t.tag.toLowerCase().includes(q));
            }
            return opts.slice(0, 10);
        },
        
        get filteredPrimaryOptions() {
            let opts = this.globalTags.filter(t => !this.mergeTagsList.includes(t.tag));
            if(this.mergePrimarySearchTerm) {
                const q = this.mergePrimarySearchTerm.toLowerCase();
                opts = opts.filter(t => t.tag.toLowerCase().includes(q));
            }
            return opts.slice(0, 10);
        },
        
        mergeMoveUp() {
            if (this.mergeSelectedIndex > 0) this.mergeSelectedIndex--;
        },
        
        mergeMoveDown() {
            if (this.mergeSelectedIndex < this.filteredMergeOptions.length - 1) this.mergeSelectedIndex++;
        },
        
        primaryMoveUp() {
            if (this.primarySelectedIndex > 0) this.primarySelectedIndex--;
        },
        
        primaryMoveDown() {
            if (this.primarySelectedIndex < this.filteredPrimaryOptions.length - 1) this.primarySelectedIndex++;
        },

        addMergeTag(tag) {
            if(!this.mergeTagsList.includes(tag)) {
                this.mergeTagsList.push(tag);
            }
            this.$nextTick(() => { this.mergeTagsDropdown = true; });
        },
        
        addMergeTagFromInput() {
            if(!this.mergeSearchTerm || this.filteredMergeOptions.length === 0) return;
            const tag = this.filteredMergeOptions[this.mergeSelectedIndex]?.tag || this.filteredMergeOptions[0].tag;
            this.addMergeTag(tag);
            this.mergeTagsDropdown = true;
            this.mergeSelectedIndex = 0;
        },
        
        setPrimaryTagFromInput() {
            if(!this.mergePrimarySearchTerm || this.filteredPrimaryOptions.length === 0) return;
            const tag = this.filteredPrimaryOptions[this.primarySelectedIndex]?.tag || this.filteredPrimaryOptions[0].tag;
            this.mergePrimaryTag = tag;
            this.mergePrimarySearchTerm = '';
            this.mergePrimaryDropdown = false;
        },
        
        removeMergeTag(tag) {
            this.mergeTagsList = this.mergeTagsList.filter(t => t !== tag);
        },
        
        getMergePath(index, total) {
            if (total === 1) return 'M 0 50 L 100 50';
            const startY = ((index + 0.5) / total) * 100;
            return `M 0 ${startY} C 35 ${startY}, 35 50, 75 50 L 100 50`;
        },
        
        async submitMerge() {
            if(!this.mergePrimaryTag || this.mergeTagsList.length === 0) return;
            if(!confirm(`Merge ${this.mergeTagsList.length} tags into "${this.mergePrimaryTag}"? This cannot be undone.`)) return;
            
            try {
                const res = await fetch('/api/tags/merge', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ primary_tag: this.mergePrimaryTag, merge_tags: this.mergeTagsList })
                });
                if (res.ok) {
                    alert("Tags merged successfully!");
                    this.mergeTagsList = [];
                    this.mergePrimaryTag = '';
                    this.mergePrimarySearchTerm = '';
                    this.fetchGlobalTags();
                } else {
                    alert("Failed to merge tags.");
                }
            } catch(e) {
                alert("Error merging tags: " + e.message);
            }
        },

        getReplyTo(tweet) {
            if (!tweet || !tweet.raw_json) return null;
            let raw = tweet.raw_json.raw_json || tweet.raw_json;
            if (raw.legacy && raw.legacy.in_reply_to_screen_name) return raw.legacy.in_reply_to_screen_name;
            if (raw.in_reply_to_screen_name) return raw.in_reply_to_screen_name;
            return null;
        },

        formatDate(dateStr, includeTime = false) {
            if (!dateStr) return '';
            const d = new Date(dateStr);
            if (includeTime) {
                return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }) + ' · ' + 
                       d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
            }
            const now = new Date();
            const diff = (now - d) / 1000;
            if (diff < 60) return 'Just now';
            if (diff < 3600) return Math.floor(diff/60) + 'm';
            if (diff < 86400) return Math.floor(diff/3600) + 'h';
            const options = { month: 'short', day: 'numeric' };
            if (d.getFullYear() !== now.getFullYear()) options.year = 'numeric';
            return d.toLocaleDateString('en-US', options);
        },
        
        formatRelativeDate(dateStr) {
            if (!dateStr) return '';
            const d = new Date(dateStr);
            const now = new Date();
            const diff = (now - d) / 1000;
            if (diff < 60) return 'just now';
            if (diff < 3600) {
                const m = Math.floor(diff/60);
                return m === 1 ? '1 min ago' : m + ' mins ago';
            }
            if (diff < 86400) {
                const h = Math.floor(diff/3600);
                return h === 1 ? '1 hr ago' : h + ' hrs ago';
            }
            const days = Math.floor(diff/86400);
            if (days < 30) return days === 1 ? '1 day ago' : days + ' days ago';
            if (days < 365) {
                const mo = Math.floor(days/30);
                return mo === 1 ? '1 mo ago' : mo + ' mos ago';
            }
            const y = Math.floor(days/365);
            return y === 1 ? '1 yr ago' : y + ' yrs ago';
        },
        
        async fetchConfig() {
            try {
                const [resSchema, resConfig] = await Promise.all([
                    fetch('/api/config/schema'),
                    fetch('/api/config')
                ]);
                if (resSchema.ok && resConfig.ok) {
                    this.configSchema = await resSchema.json();
                    this.configData = await resConfig.json();
                }
            } catch (e) {
                console.error("Error fetching config", e);
            }
        },
        
        isFieldVisible(section, key) {
            const fullKey = `${section}.${key}`;
            if (!this.configSchema || !this.configData) return false;
            if (this.configSchema.blacklist.includes(fullKey)) return false;
            if (this.configSchema.whitelist.includes(fullKey)) return true;
            return this.showAdvancedConfig;
        },
        
        hasVisibleFields(section) {
            if (!this.configSchema || !this.configData || !this.configData[section]) return false;
            return Object.keys(this.configData[section]).some(k => this.isFieldVisible(section, k));
        },
        
        getFieldType(section, key) {
            if (!this.configSchema || !this.configData) return 'text';
            const fullKey = `${section}.${key}`;
            if (this.configSchema.types && this.configSchema.types[fullKey]) return this.configSchema.types[fullKey];
            
            const val = this.configData[section][key];
            if (typeof val === 'boolean') return 'boolean';
            if (typeof val === 'number') return 'number';
            return 'text';
        },
        
        isFieldFullWidth(section, key) {
            const fullKey = `${section}.${key}`;
            if (this.configSchema && this.configSchema.full_width && this.configSchema.full_width.includes(fullKey)) return true;
            return false;
        },
        
        async saveConfig() {
            this.configSaving = true;
            try {
                const res = await fetch('/api/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(this.configData)
                });
                if (res.ok) {
                    const btn = document.getElementById('save-config-btn');
                    if (btn) {
                        const old = btn.innerHTML;
                        btn.innerHTML = "Saved!";
                        btn.classList.replace('bg-[var(--accent-color)]', 'bg-green-600');
                        setTimeout(() => {
                            btn.innerHTML = old;
                            btn.classList.replace('bg-green-600', 'bg-[var(--accent-color)]');
                        }, 2000);
                    }
                } else {
                    alert("Failed to save config: " + await res.text());
                }
            } catch (e) {
                console.error("Error saving config", e);
                alert("Error saving config.");
            } finally {
                this.configSaving = false;
            }
        },
        
        async restoreConfigDefaults() {
            if(!confirm("Are you sure you want to restore default configuration settings? This will overwrite your current settings, but your passwords and keys will be preserved.")) return;
            
            try {
                const res = await fetch('/api/config/defaults');
                if (res.ok) {
                    const defaults = await res.json();
                    if (this.configData.auth) {
                        defaults.auth.auth_token = this.configData.auth.auth_token;
                        defaults.auth.ct0 = this.configData.auth.ct0;
                        defaults.auth.user_id = this.configData.auth.user_id;
                    }
                    if (this.configData.tagging) {
                        defaults.tagging.api_key = this.configData.tagging.api_key;
                    }
                    if (this.configData.web) {
                        defaults.web.host = this.configData.web.host;
                        defaults.web.port = this.configData.web.port;
                    }
                    this.configData = defaults;
                }
            } catch (e) {
                console.error("Error fetching defaults", e);
                alert("Error fetching default config.");
            }
        },

        formatSyncDate(tweet) {
            let synced = tweet.synced_at || (tweet.collection ? tweet.collection.synced_at : null);
            if (!synced) return 'Archived';
            let dS = new Date(synced);
            return `Synced ${dS.toLocaleDateString('en-US', {month:'short', day:'numeric'})}`;
        },

        getQuoteTweet(tweet) {
            if (!tweet || this.isPlaceholderTweet(tweet)) return null;
            if (tweet.quoted_tweet) return tweet.quoted_tweet;
            if (!tweet.raw_json) return null;
            const quote = tweet.raw_json.quoted_status_result?.result;
            if (quote?.__typename === 'TweetWithVisibilityResults') {
                if (quote.birdwatch_pivot && quote.tweet) {
                    quote.tweet.birdwatch_pivot = quote.birdwatch_pivot;
                }
                return quote.tweet;
            }
            if (quote?.__typename === 'Tweet') return quote;
            if (quote?.__typename === 'TweetTombstone' || quote?.__typename === 'TweetUnavailable') return quote;
            if (tweet.raw_json.quoted_status) return tweet.raw_json.quoted_status;
            return null;
        },

        isTombstone(qt) {
            if (!qt) return false;
            return this.isPlaceholderTweet(qt) || qt.__typename === 'TweetTombstone' || qt.__typename === 'TweetUnavailable' || qt.__tombstone__ === true;
        },

        isPlaceholderTweet(tweet) {
            return Boolean(tweet?.availability?.placeholder);
        },

        renderContentPlaceholder(message, reason = 'details_not_archived') {
            const safeMessage = this.escapeHTML(message || 'Post details were not captured.');
            const safeReason = this.escapeHTML(reason);
            return `<div class="tweet-availability-placeholder mt-2 flex items-center gap-2 rounded-lg border border-[var(--border-color)] bg-[var(--bg-secondary)] px-3 py-2 text-[15px] leading-snug text-[var(--text-secondary)] whitespace-normal" role="note" data-availability-reason="${safeReason}"><svg class="h-4 w-4 flex-shrink-0 fill-current opacity-60" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm1 15h-2v-2h2v2Zm0-4h-2V7h2v6Z"></path></svg><span>${safeMessage}</span></div>`;
        },

        renderTweetPlaceholder(tweet) {
            return this.renderContentPlaceholder(
                tweet?.availability?.message || 'This post is unavailable.',
                tweet?.availability?.reason || 'unavailable_unknown',
            );
        },

        renderQuotePlaceholder(tweet) {
            const message = this.escapeHTML(
                tweet?.availability?.message || 'This quoted post is unavailable.',
            );
            const reason = this.escapeHTML(
                tweet?.availability?.reason || 'unavailable_unknown',
            );
            return `<div class="tweet-availability-placeholder tweet-quote-placeholder mt-3 flex items-center gap-2 rounded-xl border border-[var(--border-color)] px-3 py-3 text-[15px] leading-snug text-[var(--text-secondary)] whitespace-normal" role="note" data-availability-reason="${reason}"><svg class="h-4 w-4 flex-shrink-0 fill-current opacity-60" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm1 15h-2v-2h2v2Zm0-4h-2V7h2v6Z"></path></svg><span>${message}</span></div>`;
        },

        getRetweet(tweet) {
            if (!tweet || this.isPlaceholderTweet(tweet)) return null;
            if (tweet.retweeted_tweet) return tweet.retweeted_tweet;
            if (!tweet.raw_json) return null;
            const rt = tweet.raw_json.legacy?.retweeted_status_result?.result;
            if (rt?.__typename === 'TweetWithVisibilityResults') {
                if (rt.birdwatch_pivot && rt.tweet) rt.tweet.birdwatch_pivot = rt.birdwatch_pivot;
                return rt.tweet;
            }
            if (rt?.__typename === 'Tweet') return rt;
            if (tweet.raw_json.retweeted_status) return tweet.raw_json.retweeted_status;
            return null;
        },

        formatRetweetToTweet(originalTweet, rt) {
            if (rt.author) {
                return {
                    ...rt,
                    synced_at: originalTweet.synced_at,
                    collection: originalTweet.collection,
                    collections: originalTweet.collections,
                    media_tags: rt.media_tags || originalTweet.media_tags,
                };
            }
            const author = this.getQuoteAuthor(rt);
            return {
                tweet_id: rt.rest_id || rt.id_str || originalTweet.tweet_id,
                author: { display_name: author.name, username: author.screen_name, id: author.id },
                text: this.getQuoteText(rt),
                created_at: rt.legacy?.created_at || rt.created_at || originalTweet.created_at,
                synced_at: originalTweet.synced_at,
                collection: originalTweet.collection,
                raw_json: rt,
                media: originalTweet.media,
                qt_media: originalTweet.qt_media
            };
        },

        getQuoteAuthor(qt) {
            if (!qt) return { name: 'Unknown', screen_name: 'unknown', id: 'x', initial: '?' };
            if (qt.author) {
                const name = qt.author.display_name || 'Unknown';
                return {
                    name,
                    screen_name: qt.author.username || 'unknown',
                    id: qt.author.id || 'x',
                    initial: name.charAt(0),
                };
            }
            const userResult = qt.core?.user_results?.result;
            let name = userResult?.legacy?.name || userResult?.core?.name || qt.user?.name || 'Unknown';
            let screen_name = userResult?.legacy?.screen_name || userResult?.core?.screen_name || qt.user?.screen_name || 'unknown';
            let id = userResult?.rest_id || qt.user?.id_str || 'x';
            return { name, screen_name, id, initial: name.charAt(0) };
        },

        getQuoteText(qt) {
            if (!qt) return '';
            if (this.isTombstone(qt)) {
                return qt.availability?.message || qt.tombstone?.text?.text || qt.text || "This post is unavailable.";
            }
            return qt.text || qt.legacy?.full_text || qt.full_text || '';
        },

        getTweetId(tweet) {
            return tweet?.tweet_id || tweet?.rest_id || tweet?.id_str || null;
        },

        formatText(tweet, forceFull = false) {
            if (!tweet) return '';
            if (this.isPlaceholderTweet(tweet)) return this.renderTweetPlaceholder(tweet);
            const raw = tweet.raw_json?.raw_json || tweet.raw_json || tweet;
            let text = tweet.text || raw?.legacy?.full_text || raw?.full_text || '';
            if (!String(text).trim() && this.getTweetId(tweet)) {
                return this.renderContentPlaceholder(
                    'Post text was not captured in the local archive.',
                    'text_not_archived',
                );
            }
            
            if (raw && raw.legacy && raw.legacy.in_reply_to_status_id_str) {
                text = text.replace(/^(@\w+\s+)+/, '');
            }

            let isTruncated = false;
            let tweetId = tweet.tweet_id || (raw && raw.rest_id);
            
            if (!forceFull && text.length > 280 && tweetId && !this.expandedTweets[tweetId]) {
                let truncated = text.substring(0, 280);
                let lastSpace = truncated.lastIndexOf(' ');
                if (lastSpace > 200) {
                    truncated = truncated.substring(0, lastSpace);
                }
                text = truncated + '...';
                isTruncated = true;
            }
            
            let urlMap = {};
            if (raw && raw.legacy) {
                const urlsToRemove = [];
                if (raw.legacy.entities?.media) urlsToRemove.push(...raw.legacy.entities.media.map(m => m.url));
                if (raw.legacy.extended_entities?.media) urlsToRemove.push(...raw.legacy.extended_entities.media.map(m => m.url));
                if (raw.legacy.quoted_status_permalink?.url) urlsToRemove.push(raw.legacy.quoted_status_permalink.url);
                
                if (raw.card && raw.card.legacy && raw.card.legacy.url) urlsToRemove.push(raw.card.legacy.url);
                if (raw.card && raw.card.url) urlsToRemove.push(raw.card.url);
                
                urlsToRemove.filter(Boolean).forEach(u => {
                    text = text.split(u).join('');
                });
                
                if (raw.legacy.entities?.urls) {
                    raw.legacy.entities.urls.forEach(u => {
                        if (!urlsToRemove.includes(u.url)) {
                            const expandedUrl = this.safeURL(u.expanded_url);
                            const displayUrl = this.escapeHTML(u.display_url || u.expanded_url || '');
                            urlMap[u.url] = `<a href="${expandedUrl}" target="_blank" rel="noopener noreferrer" class="text-[var(--accent-color)] hover:underline" @click.stop>${displayUrl}</a>`;
                        }
                    });
                }
            }
            
            text = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            text = text.replace(/@(\w+)/g, '<span class="mention text-[var(--accent-color)] hover:underline cursor-pointer" data-user="$1">@$1</span>');
            text = text.replace(/#(\w+)/g, '<span class="hashtag text-[var(--accent-color)] hover:underline cursor-pointer" data-tag="$1">#$1</span>');
            
            Object.keys(urlMap).forEach(u => {
                text = text.split(u).join(urlMap[u]);
            });
            
            if (isTruncated) {
                text += `<span class="text-[var(--accent-color)] hover:underline cursor-pointer block mt-1 expand-tweet" data-id="${tweetId}">Show more</span>`;
            }
            
            return text.trim();
        },


        expandedTweets: {},

        handleTextClick(e) {
            if (e.target.tagName === 'SPAN' && e.target.classList.contains('mention')) {
                e.stopPropagation();
                const username = e.target.dataset.user;
                const dummyTweet = {
                    author: { username: username, display_name: username, id: 'unknown' }
                };
                this.openProfileCard(e, dummyTweet);
            } else if (e.target.tagName === 'SPAN' && e.target.classList.contains('hashtag')) {
                e.stopPropagation();
                this.searchQuery = '#' + e.target.dataset.tag;
                this.search();
            } else if (e.target.tagName === 'SPAN' && e.target.classList.contains('expand-tweet')) {
                e.stopPropagation();
                const tid = e.target.dataset.id;
                if (tid) {
                    this.expandedTweets[tid] = true;
                }
            }
        },
        
        renderCard(tweet) {
            if (this.isPlaceholderTweet(tweet)) return '';
            if (!tweet || !tweet.raw_json) return '';
            const raw = tweet.raw_json.raw_json || tweet.raw_json;
            const card = raw.card;
            if (!card) return '';
            const name = card.name || card.legacy?.name || '';
            const bindingArray = card.binding_values || card.legacy?.binding_values || [];
            const binding = {};
            if (Array.isArray(bindingArray)) {
                bindingArray.forEach(item => {
                    binding[item.key] = item.value;
                });
            } else {
                Object.assign(binding, bindingArray);
            }
            let html = '';

            if (name.startsWith('poll')) {
                const choices = [];
                for (let i = 1; i <= 4; i++) {
                    const choice = binding[`choice${i}_label`];
                    const count = binding[`choice${i}_count`];
                    if (choice && choice.string_value) {
                        choices.push({
                            label: choice.string_value,
                            count: count ? parseInt(count.string_value) : 0
                        });
                    }
                }
                if (choices.length > 0) {
                    const total = choices.reduce((s, c) => s + c.count, 0) || 1;
                    const trueTotal = choices.reduce((s, c) => s + c.count, 0);
                    const maxCount = Math.max(...choices.map(c => c.count));
                    const isFinal = binding.counts_are_final?.boolean_value === true;
                    
                    html += `<div class="mt-3 flex flex-col space-y-[6px]">`;
                    choices.forEach(c => {
                        const pct = Math.round((c.count / total) * 100);
                        const isWinner = c.count === maxCount && maxCount > 0;
                        const weightClass = isWinner ? 'font-bold' : 'font-normal';
                        const barColor = isWinner ? 'bg-[var(--accent-color)] opacity-[0.4]' : 'bg-[var(--border-color)] opacity-[0.6]';
                        const label = this.escapeHTML(c.label);
                        
                        html += `<div class="relative w-full h-[32px] rounded flex items-center overflow-hidden">
                                    <div class="absolute left-0 top-0 bottom-0 ${barColor} rounded" style="width: ${pct}%"></div>
                                    <span class="relative z-10 text-[15px] ${weightClass} text-[var(--text-primary)] w-full flex justify-between px-3">
                                        <span class="truncate pr-4" title="${label}">${label}</span>
                                        <span>${pct}%</span>
                                    </span>
                                 </div>`;
                    });
                    
                    const statusText = isFinal ? 'Final results' : '';
                    const dot = isFinal ? ' · ' : '';
                    html += `<div class="text-[14px] text-[var(--text-secondary)] mt-2">${trueTotal} votes${dot}${statusText}</div></div>`;
                }
            } else if (name === 'summary' || name === 'summary_large_image') {
                const title = this.escapeHTML(binding.title?.string_value || '');
                const desc = this.escapeHTML(binding.description?.string_value || '');
                const vanityUrl = this.escapeHTML(binding.vanity_url?.string_value || '');
                const expandedUrl = this.safeURL(
                    binding.card_url?.string_value || binding.vanity_url?.string_value || '#'
                );
                let imageHtml = '';
                
                if (name === 'summary_large_image') {
                    const imgUrl = binding.thumbnail_image_original?.image_value?.url || 
                                   binding.summary_photo_image_original?.image_value?.url || 
                                   binding.thumbnail_image_x_large?.image_value?.url ||
                                   binding.thumbnail_image?.image_value?.url || 
                                   binding.photo_image_full_size?.image_value?.url || 
                                   binding.summary_photo_image?.image_value?.url;
                    if (imgUrl) {
                        const safeImgUrl = this.safeURL(imgUrl);
                        imageHtml = `<div class="w-full aspect-[1.91/1] bg-[var(--bg-tertiary)] overflow-hidden border-b border-[var(--border-color)]">
                                        <img src="${safeImgUrl}" class="w-full h-full object-cover" loading="lazy">
                                     </div>`;
                    }
                }
                
                if (title || desc || imageHtml) {
                    html += `<a href="${expandedUrl}" target="_blank" rel="noopener noreferrer" class="mt-3 block border border-[var(--border-color)] rounded-xl overflow-hidden hover-bg transition cursor-pointer">
                                ${imageHtml}
                                <div class="p-3">
                                    <div class="text-[13px] text-[var(--text-secondary)] mb-1">${vanityUrl}</div>
                                    <div class="text-[15px] text-[var(--text-primary)] font-bold leading-tight mb-1" style="display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">${title}</div>
                                    <div class="text-[14px] text-[var(--text-secondary)]" style="display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">${desc}</div>
                                </div>
                             </a>`;
                }
            }
            return html;
        },

        renderMediaGrid(mediaList) {
            if (!mediaList || mediaList.length === 0) return '';
            
            const getSrc = (m) => {
                return m.download?.local_path ? `/${m.download.local_path}` : null;
            };

            const getPoster = (m) => {
                return m.download?.thumbnail_local_path ? `/${m.download.thumbnail_local_path}` : null;
            };

            const allMedia = mediaList.map(m => {
                const type = (m.type === 'video' || m.type === 'animated_gif') ? 'video' : 'photo';
                const isGif = m.type === 'animated_gif'; 
                const duration = m.duration_millis;
                const isShort = duration && duration <= 60000;
                return { m, src: getSrc(m), poster: getPoster(m), type, isGif, isShort };
            });
            
            if (allMedia.length === 0) return '';
            
            const downloadedMedia = allMedia.filter(x => x.src);
            const jsonStr = JSON.stringify(downloadedMedia).replace(/'/g, "&#39;").replace(/"/g, "&quot;");

            const placeholderHtml = `
                <div class="absolute inset-0 w-full h-full flex flex-col items-center justify-center bg-[var(--bg-secondary)] text-[var(--text-secondary)]">
                    <svg class="w-8 h-8 mb-2 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>
                    <span class="text-xs font-medium">Media not downloaded</span>
                </div>
            `;

            if (allMedia.length === 1) {
                const { src, poster, type, isGif, isShort } = allMedia[0];
                const aspect = allMedia[0].m.width && allMedia[0].m.height ? (allMedia[0].m.width / allMedia[0].m.height) : 0;
                const containerStyle = aspect 
                    ? `width: min(100%, calc(512px * ${aspect})); aspect-ratio: ${aspect}; max-height: 512px;`
                    : 'width: 100%; max-height: 512px; aspect-ratio: 16/9;';

                if (!src) {
                    return `<div class="mt-3 relative max-w-full rounded-2xl border border-[var(--border-color)] overflow-hidden block" style="${containerStyle}" @click.stop>
                                ${placeholderHtml}
                            </div>`;
                } else if (type === 'photo') {
                    return `<div class="mt-3 relative max-w-full rounded-2xl border border-[var(--border-color)] overflow-hidden block" style="${containerStyle}" @click.stop>
                                <img src="${src}" onclick="window.dispatchEvent(new CustomEvent('open-lightbox', { detail: { media: JSON.parse('${jsonStr}'), index: 0 } }))" class="w-full h-full object-cover cursor-pointer hover:opacity-90 transition block">
                            </div>`;
                } else {
                    const loopAttr = (isGif || isShort) ? 'loop' : '';
                    const autoplayAttr = isGif ? 'autoplay muted playsinline' : '';
                    const controlsAttr = isGif ? '' : 'controls';

                    return `<div class="mt-3 relative max-w-full rounded-2xl border border-[var(--border-color)] overflow-hidden block" style="${containerStyle}" @click.stop>
                                <video src="${src}" poster="${poster || ''}" ${autoplayAttr} ${loopAttr} ${controlsAttr} class="w-full h-full object-cover outline-none block"></video>
                            </div>`;
                }
            }
            
            let gridClass = allMedia.length === 2 ? 'grid-cols-2' : 'grid-cols-2 grid-rows-2';
            let html = `<div class="mt-3 grid gap-[2px] rounded-2xl overflow-hidden border border-[var(--border-color)] aspect-[16/9] ${gridClass}" @click.stop>`;
            
            let downloadedIdx = 0;
            allMedia.forEach((item, idx) => {
                const { src, poster, type, isGif, isShort } = item;
                let itemClass = (allMedia.length === 3 && idx === 0) ? 'row-span-2 col-span-1' : (allMedia.length === 3 ? 'col-span-1' : '');
                
                if (!src) {
                    html += `<div class="relative w-full h-full bg-[var(--border-color)] ${itemClass}">
                                ${placeholderHtml}
                            </div>`;
                } else {
                    const currentDlIdx = downloadedIdx++;
                    if (type === 'photo') {
                        html += `<div class="relative w-full h-full bg-[var(--border-color)] ${itemClass}">
                                    <img src="${src}" onclick="window.dispatchEvent(new CustomEvent('open-lightbox', { detail: { media: JSON.parse('${jsonStr}'), index: ${currentDlIdx} } }))" class="absolute inset-0 w-full h-full object-cover cursor-pointer hover:opacity-90 transition">
                                </div>`;
                    } else {
                        const loopAttr = (isGif || isShort) ? 'loop' : '';
                        const autoplayAttr = isGif ? 'autoplay muted playsinline' : '';
                        const controlsAttr = isGif ? '' : 'controls';
                        html += `<div class="relative w-full h-full bg-[var(--border-color)] ${itemClass}">
                                    <video src="${src}" poster="${poster || ''}" ${autoplayAttr} ${loopAttr} ${controlsAttr} class="absolute inset-0 w-full h-full object-cover outline-none"></video>
                                </div>`;
                    }
                }
            });
            
            html += `</div>`;
            return html;
        },

        renderRawMediaGrid(rawMediaList) {
            return this.renderMediaGrid(rawMediaList, true);
        },

        escapeHTML(str) {
            if (!str) return '';
            return String(str).replace(/&/g, '&amp;')
                      .replace(/</g, '&lt;')
                      .replace(/>/g, '&gt;')
                      .replace(/"/g, '&quot;')
                      .replace(/'/g, '&#039;');
        },

        safeURL(value) {
            if (!value || value === '#') return '#';
            try {
                const parsed = new URL(value, window.location?.origin || 'http://localhost');
                return ['http:', 'https:'].includes(parsed.protocol)
                    ? this.escapeHTML(parsed.href)
                    : '#';
            } catch (_) {
                return '#';
            }
        },

        formatCommunityNoteText(bw) {
            let text = bw.subtitle?.text || '';
            if (!text) return '';
            let entities = bw.subtitle?.entities || [];
            
            entities = [...entities].sort((a, b) => a.fromIndex - b.fromIndex);
            
            let html = '';
            let lastIndex = 0;
            
            for (const ent of entities) {
                if (ent.ref?.urlType === 'ExternalUrl' && ent.ref?.url) {
                    html += this.escapeHTML(text.substring(lastIndex, ent.fromIndex));
                    const linkText = this.escapeHTML(text.substring(ent.fromIndex, ent.toIndex));
                    const href = this.escapeHTML(ent.ref.url);
                    html += `<a href="${href}" target="_blank" class="text-[var(--accent-color)] hover:underline" @click.stop>${linkText}</a>`;
                    lastIndex = ent.toIndex;
                }
            }
            html += this.escapeHTML(text.substring(lastIndex));
            return html;
        },

        renderCommunityNote(raw_json, isQuote = false) {
            raw_json = raw_json?.raw_json || raw_json;
            if (!raw_json || !raw_json.birdwatch_pivot) return '';
            const bw = raw_json.birdwatch_pivot;
            if (!bw.subtitle || !bw.subtitle.text) return '';
            
            const textHtml = this.formatCommunityNoteText(bw);
            const containerClass = isQuote 
                ? "-mx-3 -mb-3 p-3 bg-[var(--bg-secondary)] border-t border-[var(--border-color)] rounded-b-xl text-left" 
                : "mt-3 border border-[var(--border-color)] rounded-xl p-3 bg-[var(--bg-secondary)] hover-bg transition cursor-pointer text-left";

            const iconSvg = `<svg viewBox="0 0 24 24" fill="var(--accent-color)" class="w-[18px] h-[18px]"><path fill-rule="evenodd" d="M8.25 6.75a3.75 3.75 0 117.5 0 3.75 3.75 0 01-7.5 0zM15.75 9.75a3 3 0 116 0 3 3 0 01-6 0zM2.25 9.75a3 3 0 116 0 3 3 0 01-6 0zM6.31 15.117A6.745 6.745 0 0112 12a6.745 6.745 0 016.709 7.498.75.75 0 01-.372.568A12.696 12.696 0 0112 21.75c-2.305 0-4.47-.612-6.337-1.684a.75.75 0 01-.372-.568 6.787 6.787 0 011.019-4.38z" clip-rule="evenodd" /><path d="M5.082 14.254a8.287 8.287 0 00-1.308 5.135 9.687 9.687 0 01-1.764-.44l-.115-.04a.563.563 0 01-.373-.487l-.01-.121a3.75 3.75 0 016.576-3.036c.32.338.608.708.857 1.103a6.732 6.732 0 00-3.863 1.341 6.772 6.772 0 01-.004-3.456z" /><path d="M18.918 14.254a8.287 8.287 0 011.308 5.135 9.687 9.687 0 001.764-.44l.115-.04a.563.563 0 00.373-.487l-.01-.121a3.75 3.75 0 00-6.576-3.036c-.32.338-.608.708-.857 1.103a6.732 6.732 0 013.863 1.341 6.772 6.772 0 00.004-3.456z" /></svg>`;
            
            return `
            <div class="${containerClass}" ${!isQuote ? 'onclick="event.stopPropagation()"' : ''}>
                <div class="flex items-center space-x-2 text-[15px] font-bold text-[var(--text-primary)] mb-1">
                    ${iconSvg}
                    <span>${this.escapeHTML(bw.shorttitle || 'Readers added context')}</span>
                </div>
                <div class="text-[15px] text-[var(--text-primary)] whitespace-pre-wrap break-words leading-normal mt-2">${textHtml}</div>
            </div>`;
        },

        renderActionBar(tweet, isMain = false) {
            if (this.isPlaceholderTweet(tweet)) return '';
            const legacy = tweet.raw_json?.legacy || {};
            const replyCount = legacy.reply_count || 0;
            const retweetCount = (legacy.retweet_count || 0) + (legacy.quote_count || 0);
            const likeCount = legacy.favorite_count || 0;
            const viewCount = tweet.raw_json?.views?.count || 0;
            const bmkCount = legacy.bookmark_count || 0;
            
            const formatNum = (num) => num > 0 ? (num > 999 ? (num/1000).toFixed(1) + 'K' : num) : '';
            
            const collections = tweet.collections || [];
            if (tweet.collection?.type) collections.push(tweet.collection.type);
            const isLiked = collections.includes('like');
            const isBookmarked = collections.includes('bookmark');
            
            const likeIcon = isLiked ? 
                `<svg viewBox="0 0 24 24" class="w-[18.5px] h-[18.5px]" style="fill: var(--danger-color)"><path d="M20.884 13.19c-1.351 2.48-4.001 5.12-8.379 7.67l-.503.3-.504-.3C7.121 18.31 4.471 15.67 3.119 13.19 1.928 10.99 1.898 8.48 2.921 6.45 3.864 4.56 5.8 3.32 8.016 3.42c1.474.07 2.812.8 3.486 2.08l.498.94.498-.94c.674-1.28 2.012-2.01 3.486-2.08 2.216-.1 4.152 1.14 5.095 3.03 1.023 2.03.993 4.54-.195 6.74z"></path></svg>` : 
                `<svg viewBox="0 0 24 24" class="w-[18.5px] h-[18.5px] fill-current group-hover:fill-[var(--danger-color)]"><path d="M16.697 5.5c-1.222-.06-2.679.51-3.89 2.16l-.805 1.09-.806-1.09C9.984 6.01 8.526 5.44 7.304 5.5c-1.243.07-2.349.78-2.91 1.91-.552 1.12-.633 2.78.479 4.82 1.074 1.97 3.257 4.27 7.129 6.61 3.87-2.34 6.052-4.64 7.126-6.61 1.111-2.04 1.03-3.7.477-4.82-.561-1.13-1.666-1.84-2.908-1.91zm4.187 7.69c-1.351 2.48-4.001 5.12-8.379 7.67l-.503.3-.504-.3c-4.379-2.55-7.029-5.19-8.382-7.67-1.36-2.5-1.41-4.86-.514-6.67.887-1.79 2.647-2.91 4.601-3.01 1.651-.09 3.368.56 4.798 2.01 1.429-1.45 3.146-2.1 4.796-2.01 1.954.1 3.714 1.22 4.601 3.01.896 1.81.846 4.17-.514 6.67z"></path></svg>`;

            const bmkIcon = isBookmarked ? 
                `<svg viewBox="0 0 24 24" class="w-[18.5px] h-[18.5px] fill-[var(--accent-color)]"><path d="M4 4.5C4 3.12 5.119 2 6.5 2h11C18.881 2 20 3.12 20 4.5v18.44l-8-5.71-8 5.71V4.5z"></path></svg>` : 
                `<svg viewBox="0 0 24 24" class="w-[18.5px] h-[18.5px] fill-current"><path d="M4 4.5C4 3.12 5.119 2 6.5 2h11C18.881 2 20 3.12 20 4.5v18.44l-8-5.71-8 5.71V4.5zM6.5 4c-.276 0-.5.22-.5.5v14.56l6-4.29 6 4.29V4.5c0-.28-.224-.5-.5-.5H6.5z"></path></svg>`;

            const margin = isMain ? 'pt-3 pb-0 border-t border-[var(--border-color)] w-full' : 'mt-3 w-full';
            
            return `
            <div class="flex items-center justify-between text-[var(--text-secondary)] w-full ${margin}">
                <div class="flex items-center">
                    <svg viewBox="0 0 24 24" class="w-[18.5px] h-[18.5px] fill-current"><path d="M1.751 10c0-4.42 3.584-8 8.005-8h4.366c4.49 0 8.129 3.64 8.129 8.13 0 2.96-1.607 5.68-4.196 7.11l-8.054 4.46v-3.69h-.067c-4.49.1-8.183-3.51-8.183-8.01zm8.005-6c-3.317 0-6.005 2.69-6.005 6 0 3.37 2.77 6.08 6.138 6.01l.351-.01h1.761v2.3l5.087-2.81c1.951-1.08 3.163-3.13 3.163-5.36 0-3.39-2.744-6.13-6.129-6.13H9.756z"></path></svg>
                    <span class="text-[13px] ml-2 font-medium">${formatNum(replyCount)}</span>
                </div>
                <div class="flex items-center">
                    <svg viewBox="0 0 24 24" class="w-[18.5px] h-[18.5px] fill-current"><path d="M4.5 3.88l4.432 4.14-1.364 1.46L5.5 7.55V16c0 1.1.896 2 2 2H13v2H7.5c-2.209 0-4-1.79-4-4V7.55L1.432 9.48.068 8.02 4.5 3.88zM16.5 6H11V4h5.5c2.209 0 4 1.79 4 4v8.45l2.068-1.93 1.364 1.46-4.432 4.14-4.432-4.14 1.364-1.46 2.068 1.93V8c0-1.1-.896-2-2-2z"></path></svg>
                    <span class="text-[13px] ml-2 font-medium">${formatNum(retweetCount)}</span>
                </div>
                <div class="flex items-center group cursor-pointer ${isLiked ? 'text-[var(--danger-color)]' : 'hover:text-[var(--danger-color)]'}">
                    ${likeIcon}
                    <span class="text-[13px] ml-2 font-medium">${formatNum(likeCount)}</span>
                </div>
                <div class="flex items-center group cursor-pointer ${isBookmarked ? 'text-[var(--accent-color)]' : 'hover:text-[var(--accent-color)]'}">
                    ${bmkIcon}
                    <span class="text-[13px] ml-2 font-medium">${formatNum(bmkCount)}</span>
                </div>
                <div class="flex items-center">
                    <svg viewBox="0 0 24 24" class="w-[18.5px] h-[18.5px] fill-current"><path d="M8.75 21V3h2v18h-2zM18 21V8.5h2V21h-2zM4 21l.004-10h2L6 21H4zm9.248 0v-7h2v7h-2z"></path></svg>
                    <span class="text-[13px] ml-2 font-medium">${formatNum(viewCount)}</span>
                </div>
                <div class="flex items-center">
                    <a href="https://x.com/${tweet.author?.username || 'i'}/status/${tweet.tweet_id}" target="_blank" @click.stop class="flex items-center" title="Open on Twitter">
                        <svg viewBox="0 0 24 24" class="w-[18.5px] h-[18.5px] fill-current hover:text-[#e7e9ea] transition"><path d="M18 19H6c-.55 0-1-.45-1-1V6c0-.55.45-1 1-1h5c.55 0 1-.45 1-1s-.45-1-1-1H6c-1.65 0-3 1.35-3 3v12c0 1.65 1.35 3 3 3h12c1.65 0 3-1.35 3-3v-5c0-.55-.45-1-1-1s-1 .45-1 1v5c0 .55-.45 1-1 1zM14 4c0 .55.45 1 1 1h2.59l-9.13 9.13c-.39.39-.39 1.02 0 1.41.19.19.45.29.71.29s.51-.1.71-.29L19 6.41V9c0 .55.45 1 1 1s1-.45 1-1V4c0-.55-.45-1-1-1h-5c-.55 0-1 .45-1 1z"></path></svg>
                    </a>
                </div>
            </div>`;
        }
    };
}
