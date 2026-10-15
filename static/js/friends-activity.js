/**
 * Friends Activity Manager
 * Displays what friends have done with a specific media item.
 *
 * F35 — DOM-XSS remediation.
 *
 * This file previously built every row by string concatenation with NO escaping
 * whatsoever. Every value it interpolated was attacker-controlled:
 *
 *   * `username`  — set at registration. `routes/auth.py` applied no character
 *                   validation beyond uniqueness, so a quote in a username
 *                   terminated the enclosing attribute and injected a live
 *                   event handler.
 *   * `content`   — user-written review and comment text.
 *   * `tags`      — user-created tag names.
 *   * `user_avatar` — a URL rendered straight into `src`.
 *
 * Nothing here is escaped-and-concatenated any more. Rows are built with
 * `FrameIQDOM.el()`, which assigns untrusted values through `textContent` (it
 * has no `html` option at all) and routes `href`/`src` through the URL
 * validators. Behaviour moved from string-built to delegated listeners, so no
 * inline JavaScript is generated from a data value.
 *
 * Every helper degrades closed: if the shared library is missing, or a value
 * fails validation, the affected node is omitted or rendered as inert text
 * rather than as an executable attribute.
 */
class FriendsActivityManager {
    constructor(mediaId, mediaType) {
        this.mediaId = mediaId;
        this.mediaType = mediaType;
        this.container = null;
    }

    /** Lazily resolve the shared DOM-safety library. */
    dom() {
        return window.FrameIQDOM;
    }

    /**
     * A bounded integer, or null.
     *
     * The activity summary interpolates counts straight into text; a
     * non-numeric value there would print `undefined` or, worse, be a value we
     * never intended to display.
     */
    count(value) {
        const n = Number(value);
        return Number.isInteger(n) && n >= 0 && n <= 999999 ? n : null;
    }

    /**
     * A 0–10 rating, clamped.
     *
     * `'★'.repeat(review.rating)` throws a RangeError on a negative count and
     * silently renders nothing sensible on a float, so the value is coerced
     * before it reaches `repeat`.
     */
    stars(rating) {
        const n = Number(rating);
        if (!Number.isInteger(n) || n <= 0) return '';
        const filled = Math.min(n, 10);
        return '★'.repeat(filled) + '☆'.repeat(10 - filled);
    }

    /**
     * A `/profile/<id>` link, or a plain span when the id is not a safe integer.
     *
     * Fail-closed: an id we cannot validate produces no `href` at all rather
     * than a link built from an interpolated string.
     */
    profileLink(D, userId, username, className) {
        const id = D.safeId(userId);
        if (id === null) {
            return D.el('span', { class: className, text: D.text(username) });
        }
        return D.el('a', {
            class: className,
            href: '/profile/' + id,
            text: D.text(username),
        });
    }

    /** An avatar image with a validated src and a safe fallback. */
    avatar(D, url, alt, className) {
        return D.el('img', {
            class: className,
            src: D.safeImageUrl(url, '/static/images/default-avatar.png'),
            alt: D.text(alt),
        });
    }

    async init(containerId) {
        this.container = document.getElementById(containerId);
        if (!this.container) {
            return;
        }

        await this.loadActivity();
    }

    async loadActivity() {
        try {
            const response = await fetch(
                `/api/media/${this.mediaId}/${this.mediaType}/friends-activity`
            );
            const data = await response.json();

            if (data.success) {
                this.renderActivity(data);
            } else {
                this.container.innerHTML = '<p>Unable to load friends activity</p>';
            }
        } catch (error) {
            this.container.innerHTML = '<p>Error loading friends activity</p>';
        }
    }

    renderActivity(data) {
        const D = this.dom();
        if (!D) return;   // fail closed: render nothing rather than something unsafe

        const activity = data.activity || {};

        if (!data.has_friends) {
            // Static markup only — no interpolated value, so a string build is safe.
            this.container.innerHTML = `
                <div class="no-friends-activity">
                    <p>Follow other users to see what your friends think!</p>
                    <a href="/discover-users" class="btn-discover">Discover Users</a>
                </div>
            `;
            return;
        }

        const groups = [
            ['reviews', '📝 Reviews', this.reviewRows(D, activity.reviews)],
            ['watched', '✓ Watched', this.avatarRows(D, activity.watched)],
            ['watchlisted', '📋 On Watchlist', this.avatarRows(D, activity.watchlisted)],
            ['likes', '❤️ Likes', this.avatarRows(D, activity.likes)],
            ['tags', '🏷️ Tags', this.tagRows(D, activity.tags)],
        ].filter(([, , nodes]) => nodes && nodes.length);

        if (groups.length === 0) {
            this.container.innerHTML = `
                <div class="no-friends-activity">
                    <p>None of your friends have interacted with this yet.</p>
                    <p class="hint">Be the first to review it!</p>
                </div>
            `;
            return;
        }

        const engaged = this.count(data.engaging_friends_count);
        const total = this.count(data.friends_count);

        const header = D.el('div', { class: 'friends-activity-header' }, [
            D.el('h3', { text: '👥 Friends Activity' }),
            D.el('p', {
                class: 'activity-summary',
                text: engaged === null || total === null
                    ? ''
                    : `${engaged} of your ${total} friends have interacted with this`,
            }),
        ]);

        const frag = document.createDocumentFragment();
        frag.appendChild(header);
        groups.forEach(([, title, nodes]) => {
            frag.appendChild(D.el('div', { class: 'activity-section' }, [
                D.el('h4', { text: title }),
                ...nodes,
            ]));
        });

        this.container.innerHTML = '';
        this.container.appendChild(frag);
    }

    /** A review row: avatar, name link, rating, review text, timestamp. */
    reviewRows(D, reviews) {
        if (!Array.isArray(reviews)) return [];
        return reviews.map(review => {
            const info = D.el('div', { class: 'friend-info' }, [
                this.avatar(D, review.user_avatar, review.username, 'friend-avatar'),
                D.el('div', {}, [
                    this.profileLink(D, review.user_id, review.username, 'friend-name'),
                    ...(this.stars(review.rating)
                        ? [D.el('div', { class: 'friend-rating', text: this.stars(review.rating) })]
                        : []),
                ]),
            ]);

            return D.el('div', { class: 'friend-activity-item' }, [
                info,
                D.el('p', { class: 'friend-review', text: D.text(review.content) }),
                D.el('span', { class: 'activity-time', text: this.formatTime(review.created_at) }),
            ]);
        });
    }

    /**
     * An avatar row, used for watched / watchlisted / likes.
     *
     * `title` was previously interpolated unescaped, so a quote in a username
     * broke out of the attribute; `title` is now a plain property assignment.
     */
    avatarRows(D, users) {
        if (!Array.isArray(users) || users.length === 0) return [];
        const wrap = D.el('div', { class: 'friend-avatars' });
        users.forEach(user => {
            const rating = this.count(user.rating);
            const title = rating === null
                ? D.text(user.username)
                : `${D.text(user.username)} - ${rating}/10`;

            const item = D.el('div', { class: 'friend-avatar-item' });
            item.title = title;
            item.appendChild(this.avatar(D, user.user_avatar, user.username, ''));
            item.appendChild(D.el('span', { text: D.text(user.username) }));
            wrap.appendChild(item);
        });
        return [wrap];
    }

    /** Tag rows: name link followed by the user's tag names as text. */
    tagRows(D, tagGroups) {
        if (!Array.isArray(tagGroups)) return [];
        return tagGroups.map(entry => {
            const tags = Array.isArray(entry.tags) ? entry.tags : [];
            const pills = tags.map(tag => D.el('span', {
                class: 'friend-tag',
                text: D.text(tag),
            }));

            return D.el('div', { class: 'friend-tags' }, [
                this.profileLink(D, entry.user_id, entry.username, 'friend-name'),
                document.createTextNode(': '),
                ...pills,
            ]);
        });
    }

    formatTime(timestamp) {
        const date = new Date(timestamp);
        if (Number.isNaN(date.getTime())) return '';
        const now = new Date();
        const seconds = Math.floor((now - date) / 1000);

        if (seconds < 60) return 'Just now';
        if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
        if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
        if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
        return date.toLocaleDateString();
    }
}

// Auto-initialize if data attributes are present
document.addEventListener('DOMContentLoaded', () => {
    const container = document.querySelector('[data-friends-activity]');
    if (container) {
        const mediaId = container.dataset.mediaId;
        const mediaType = container.dataset.mediaType;
        const containerId = container.id;

        if (mediaId && mediaType) {
            const manager = new FriendsActivityManager(mediaId, mediaType);
            manager.init(containerId);
        }
    }
});
