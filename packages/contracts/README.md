# @repo/contracts

Shared oRPC contracts and Zod schemas for type-safe API communication across apps.

## Tech Stack

- **Contracts**: oRPC (`@orpc/contract`) for route definitions
- **Validation**: Zod schemas for input/output validation
- **Type Safety**: Inferred TypeScript types from schemas

## Structure

```
packages/contracts/src/
├── index.ts                # Public exports
├── contracts.ts            # Central contract registry (version re-exports)
└── modules/
    └── v1/
        ├── v1.contract.ts         # Version router (assembles features + applies /v1 prefix)
        └── [feature]/
            ├── [feature].schema.ts    # Zod schemas and types
            └── [feature].contract.ts  # oRPC route definitions
```

### Key Pattern: Schema + Contract

Each feature has two files:

- **Schema** (`[feature].schema.ts`) — Zod schemas, inferred types, and validation logic
- **Contract** (`[feature].contract.ts`) — oRPC route definitions using the schemas

## Usage

### Defining Schemas

```typescript
// modules/v1/users/users.schema.ts
import { z } from "zod"

export const UserSchema = z.object({
	id: z.number().int().positive(),
	email: z.string().email(),
	name: z.string().min(1).max(255),
})

export const CreateUserSchema = UserSchema.pick({ email: true, name: true })

export type User = z.infer<typeof UserSchema>
export type CreateUser = z.infer<typeof CreateUserSchema>
```

### Defining Contracts

```typescript
// modules/v1/users/users.contract.ts
import { oc } from "@orpc/contract"
import { z } from "zod"

import { CreateUserSchema, UserSchema } from "./users.schema.js"

export const userContract = {
	list: oc.route({ method: "GET", path: "/users", tags: ["Users"] }).output(z.array(UserSchema)),

	create: oc
		.route({ method: "POST", path: "/users", tags: ["Users"] })
		.input(CreateUserSchema)
		.output(UserSchema),
}
```

### Wiring Into the Version Router

```typescript
// modules/v1/v1.contract.ts
import { oc } from "@orpc/contract"

import { userContract } from "./users/users.contract.js"

export const v1Contract = oc.prefix("/v1").router(
	oc.router({
		user: userContract,
		// ...other feature contracts
	})
)

export type V1Contract = typeof v1Contract
```

Then re-export from `contracts.ts`:

```typescript
export { v1Contract, type V1Contract } from "./modules/v1/v1.contract.js"
```

### Backend Usage (NestJS)

```typescript
import { Implement } from "@orpc/nest"

import { v1 } from "@/config/api-versions.config"

@Controller()
export class UsersController {
	@Implement(v1.user.list)
	async list() {
		return implement(v1.user.list).handler(async () => {
			return this.service.findAll()
		})
	}
}
```

### Frontend Usage (Next.js)

```typescript
import { OpenAPILink } from "@orpc/openapi-client/fetch"

import { v1Contract } from "@repo/contracts"

// The contract provides full type safety for API calls
const link = new OpenAPILink(v1Contract, { url: "http://localhost:3000/api" })
```

## Adding a New Feature Contract

1. Create the schema file (`modules/v1/[feature]/[feature].schema.ts`)
2. Create the contract file (`modules/v1/[feature]/[feature].contract.ts`)
3. Add the contract to the version router in `modules/v1/v1.contract.ts`
4. Export schemas from `index.ts`

## Benefits

- **Single source of truth** — Contracts define routes, validation, and TypeScript types
- **Auto-generated docs** — OpenAPI specs generated directly from oRPC contracts
- **End-to-end type safety** — Same contract types shared between backend and frontend
- **Version isolation** — Each API version has its own contract router
