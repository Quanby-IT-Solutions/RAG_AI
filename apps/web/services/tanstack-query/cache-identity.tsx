import { AnalyticsIdentityStamp } from "@/services/analytics/analytics-identity-stamp"
import { getSession } from "@/services/better-auth/auth-server"
import { CacheIdentityStamp } from "@/services/tanstack-query/provider"

/**
 * Binds the persisted query cache to the signed-in user.
 *
 * Render this once per authenticated subtree. It resolves the session on the
 * server and hands the id to the client stamp, which scopes every IndexedDB
 * bucket to that user.
 *
 * Deliberately *not* in the root layout: `getSession` reads cookies, so placing
 * it there opts every route into dynamic rendering — including `/login` and the
 * PWA's `/~offline` fallback, which must stay prerenderable. Every surface that
 * mounts this is already dynamic because it resolves a session of its own, and
 * `getSession` is React-cached, so mounting this costs those routes no extra
 * request.
 *
 * This app's `getSession` returns `null` on any failure rather than throwing, so
 * a failed read lands on the anonymous bucket — no identity to separate.
 */
export async function CacheIdentity() {
	const session = await getSession()
	const userId = session?.user?.id ?? null

	// PostHog's identity follows the same session read, so the two can never
	// disagree about who is signed in.
	return (
		<>
			<CacheIdentityStamp userId={userId} />
			<AnalyticsIdentityStamp userId={userId} />
		</>
	)
}
