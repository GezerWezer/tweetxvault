/**
 * Search autocomplete component for tweetxvault Web UI.
 */

function searchAutocomplete() {
    return {
        showDropdown: false,
        selectedIndex: 0,
        options: [],
        cursorPos: 0,
        
        allFilters: [
            { prefix: 'from:', desc: 'Sent from a specific user' },
            { prefix: 'to:', desc: 'Replying to a specific user' },
            { prefix: 'has:', desc: 'Includes specific type of media' },
            { prefix: 'filter:', desc: 'Filter by tweet type' },
            { prefix: 'min_faves:', desc: 'Minimum likes' },
            { prefix: 'min_retweets:', desc: 'Minimum retweets' },
            { prefix: 'since:', desc: 'After a specific date (YYYY-MM-DD)' },
            { prefix: 'until:', desc: 'Before a specific date (YYYY-MM-DD)' },
            { prefix: 'url:', desc: 'Contains a specific URL' },
            { prefix: 'tag:', desc: 'Matches media tag' }
        ],
        
        hasOptions: [
            { prefix: 'has:', value: 'media', desc: 'Images, videos, or GIFs' },
            { prefix: 'has:', value: 'image', desc: 'Only images' },
            { prefix: 'has:', value: 'video', desc: 'Videos or GIFs' },
            { prefix: 'has:', value: 'links', desc: 'External links' }
        ],
        
        filterOptions: [
            { prefix: 'filter:', value: 'media', desc: 'Any media' },
            { prefix: 'filter:', value: 'images', desc: 'Only images' },
            { prefix: 'filter:', value: 'videos', desc: 'Videos or GIFs' },
            { prefix: 'filter:', value: 'links', desc: 'External links' },
            { prefix: 'filter:', value: 'replies', desc: 'Replies to other tweets' },
            { prefix: 'filter:', value: 'quote', desc: 'Quote tweets' },
            { prefix: 'filter:', value: 'nativeretweets', desc: 'Retweets' },
            { prefix: 'filter:', value: 'self_threads', desc: 'Self-reply threads' },
            { prefix: 'filter:', value: 'verified', desc: 'From verified users' }
        ],
        
        handleInput() {
            const input = this.$refs.searchInput;
            this.cursorPos = input.selectionStart;
            
            const textToCursor = this.searchQuery.substring(0, this.cursorPos);
            const words = textToCursor.split(/\s+/);
            const currentWord = words[words.length - 1] || '';
            
            this.showDropdown = true;
            this.selectedIndex = 0;
            
            if (currentWord.includes(':')) {
                const parts = currentWord.split(':');
                const rawPrefix = parts[0] + ':';
                const basePrefix = rawPrefix.startsWith('-') ? rawPrefix.substring(1) : rawPrefix;
                const val = parts.slice(1).join(':').toLowerCase();
                
                if (basePrefix === 'has:') {
                    this.options = this.hasOptions.filter(o => o.value.startsWith(val)).map(o => ({...o, prefix: rawPrefix}));
                } else if (basePrefix === 'filter:') {
                    this.options = this.filterOptions.filter(o => o.value.startsWith(val)).map(o => ({...o, prefix: rawPrefix}));
                } else if (basePrefix === 'from:' || basePrefix === 'to:') {
                    if (val.length > 0) {
                        fetch(`/api/authors/search?q=${encodeURIComponent(val)}`)
                            .then(res => res.ok ? res.json() : { authors: [] })
                            .then(data => {
                                const authorsList = data && data.authors ? data.authors : [];
                                this.options = authorsList.map(a => ({
                                    isAuthor: true,
                                    prefix: rawPrefix,
                                    value: a.username,
                                    desc: a.display_name,
                                    id: a.id,
                                    query: val
                                }));
                                if (this.options.length === 0) {
                                    this.showDropdown = false;
                                }
                            })
                            .catch(err => {
                                console.error(err);
                                this.options = [];
                                this.showDropdown = false;
                            });
                    } else {
                        this.options = [];
                        this.showDropdown = false;
                    }
                } else if (basePrefix === 'tag:') {
                    fetch(`/api/tags/autocomplete?q=${encodeURIComponent(val)}`)
                        .then(res => res.ok ? res.json() : { tags: [] })
                        .then(data => {
                            const tagsList = data && data.tags ? data.tags : [];
                            this.options = tagsList.map(t => ({
                                isTag: true,
                                prefix: rawPrefix,
                                value: t.tag,
                                count: t.count.toLocaleString()
                            }));
                            if (this.options.length === 0) {
                                this.showDropdown = false;
                            }
                        })
                        .catch(err => {
                            console.error(err);
                            this.options = [];
                            this.showDropdown = false;
                        });
                } else {
                    this.options = [];
                    this.showDropdown = false;
                }
            } else {
                if (currentWord.startsWith('-')) {
                    const cleanWord = currentWord.substring(1).toLowerCase();
                    this.options = this.allFilters.filter(o => o.prefix.startsWith(cleanWord)).map(o => ({...o, prefix: '-' + o.prefix}));
                } else {
                    const lowerWord = currentWord.toLowerCase();
                    this.options = this.allFilters.filter(o => o.prefix.startsWith(lowerWord));
                }
            }
        },
        
        moveDown() {
            if (!this.showDropdown || this.options.length === 0) return;
            this.selectedIndex = (this.selectedIndex + 1) % this.options.length;
        },
        
        moveUp() {
            if (!this.showDropdown || this.options.length === 0) return;
            this.selectedIndex = (this.selectedIndex - 1 + this.options.length) % this.options.length;
        },
        
        selectOptionIndex(idx) {
            this.selectedIndex = idx;
            this.selectOption();
        },
        
        selectOption() {
            if (!this.showDropdown || this.options.length === 0) return;
            
            const opt = this.options[this.selectedIndex];
            const textToCursor = this.searchQuery.substring(0, this.cursorPos);
            const textAfterCursor = this.searchQuery.substring(this.cursorPos);
            
            const words = textToCursor.split(/\s+/);
            words.pop();
            
            let valueToInsert = opt.value;
            if (valueToInsert && valueToInsert.includes(' ')) {
                valueToInsert = `"${valueToInsert}"`;
            }
            
            let insertion = opt.prefix + (valueToInsert ? valueToInsert + ' ' : '');
            
            const newTextToCursor = (words.length > 0 ? words.join(' ') + ' ' : '') + insertion;
            this.searchQuery = newTextToCursor + textAfterCursor;
            
            this.showDropdown = false;
            
            this.$nextTick(() => {
                this.$refs.searchInput.focus();
                this.$refs.searchInput.setSelectionRange(newTextToCursor.length, newTextToCursor.length);
                if (opt.value || opt.prefix.endsWith(':')) {
                    this.handleInput();
                }
            });
        },
        
        highlightMatch(text, query) {
            if (!query || !text) return text || '';
            const cleanQuery = query.startsWith('@') ? query.substring(1) : query;
            if (!cleanQuery) return text;
            const idx = text.toLowerCase().indexOf(cleanQuery.toLowerCase());
            if (idx === -1) return text;
            return text.substring(0, idx) + '<b>' + text.substring(idx, idx + cleanQuery.length) + '</b>' + text.substring(idx + cleanQuery.length);
        }
    };
}

