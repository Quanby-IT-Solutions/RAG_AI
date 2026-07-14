---
description: Co-locate types with their usage instead of separate type-only files
globs: ["apps/**/*.ts", "apps/**/*.tsx"]
alwaysApply: true
---

# Co-locate Types Rule

## Rule: Avoid Separate Type-Only Files

**Guideline:** Do NOT create separate files that only contain type definitions. Instead, co-locate types with the code that uses them.

**Scope:** This rule applies to application code in `apps/`. Shared packages (`packages/`) have different requirements.

### What are Type-Only Files?

Type-only files are files that contain only TypeScript type definitions, interfaces, or enums without any runtime code:

```typescript
// ❌ BAD - Separate type-only file (types/user.types.ts)
export interface User {
	id: string
	name: string
	email: string
}

export type UserRole = "admin" | "user" | "guest"

export interface UserPreferences {
	theme: "light" | "dark"
	notifications: boolean
}
```

### Why Avoid Separate Type-Only Files?

1. **Context Loss**: Types are separated from the code that uses them
2. **Navigation Overhead**: Developers must jump between files to understand types
3. **Maintenance Burden**: Types and implementation can get out of sync
4. **Import Complexity**: Unnecessary import statements for simple types
5. **File Proliferation**: Creates many small files that could be consolidated

### Preferred Approach: Co-locate Types

**✅ GOOD - Types with their usage:**

```typescript
// components/user-profile.tsx
interface User {
	id: string
	name: string
	email: string
}

type UserRole = "admin" | "user" | "guest"

export function UserProfile({ user }: { user: User }) {
	// Component implementation using User type
}
```

**✅ GOOD - Types in the same feature directory:**

```typescript
// features/user-management/api/user.router.ts
interface CreateUserRequest {
	name: string
	email: string
	role: UserRole
}

export const userRouter = createTRPCRouter({
	create: publicProcedure
		.input(
			z.object({
				name: z.string(),
				email: z.string().email(),
				role: z.enum(["admin", "user", "guest"]),
			})
		)
		.mutation(async ({ input }) => {
			// Implementation using CreateUserRequest type
		}),
})
```

**✅ GOOD - Shared types in the same module:**

```typescript
// features/user-management/lib/user.utils.ts
export interface User {
	id: string
	name: string
	email: string
}

export type UserRole = "admin" | "user" | "guest"

export function formatUserName(user: User): string {
	return user.name
}
```

### Exceptions

Separate type files are acceptable in these cases:

#### Monorepo Package Exceptions

1. **`packages/contracts/`** - Designed specifically for shared API contracts, Zod schemas, and DTOs that are consumed by multiple apps
2. **`packages/db/src/schema/`** - Drizzle schema definitions that define database structure
3. **`packages/auth/src/types.ts`** - Auth-related type re-exports for external consumption

#### General Exceptions

4. **Shared across 3+ features** - Types used by many distinct features within an app
5. **Generated types** - Auto-generated from OpenAPI/Swagger, Drizzle, or external APIs
6. **Third-party integrations** - External service type definitions that don't belong to a single feature

### File Organization (within apps)

- Put types in the same file as the component/function that uses them
- For shared types within a feature, put them in the feature's `lib/` directory
- For truly shared types across features, put them in `core/lib/`
- Use descriptive filenames that indicate the primary purpose, not just "types"

### Enforcement (within apps)

- No `*.types.ts` files should exist in `apps/`
- No `types/` directories with only type definitions in `apps/`
- Types should be defined close to where they're first used
- Import shared types from `@repo/contracts` when needed across apps

---

**Remember:** Types are most valuable when they're close to the code that uses them, making the codebase more maintainable and easier to understand. For cross-app type sharing, use `@repo/contracts`.
