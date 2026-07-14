import { createAuthClient } from "better-auth/react"

import { getAuthUrl } from "./lib/utils"

/**
 * Better Auth client for React components.
 * Provides hooks like useSession, signIn, signOut, etc.
 *
 * Points to the NestJS backend for auth operations.
 */
export const authClient = createAuthClient({
	baseURL: getAuthUrl(),
}) as ReturnType<typeof createAuthClient>
