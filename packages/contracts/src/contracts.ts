/**
 * Central contract registry
 * Re-exports version routers
 */

// V1 contracts (routers)
export { v1Contract } from "./modules/v1/v1.contract.js"
export type { V1Contract } from "./modules/v1/v1.contract.js"

// Example todos schemas + types
export {
	TodoSchema,
	CreateTodoSchema,
	UpdateTodoRequestSchema,
} from "./modules/v1/examples/todos/todos.schema.js"
export type {
	Todo,
	CreateTodoInput,
	UpdateTodoRequest,
} from "./modules/v1/examples/todos/todos.schema.js"

// RBAC catalog (shared constants, enums, types)
export * from "./modules/v1/rbac/rbac.catalog.js"

// RBAC administration contract + schemas
export { rbacContract } from "./modules/v1/rbac/rbac.contract.js"
export {
	RoleSchema,
	CreateRoleSchema,
	UpdateRoleRequestSchema,
	SetRolePermissionsRequestSchema,
	RoleIdSchema,
	DeleteRoleResponseSchema,
	PermissionSchema,
	UserWithRolesSchema,
	AssignRoleRequestSchema,
	RemoveRoleRequestSchema,
	UserRoleMutationResponseSchema,
} from "./modules/v1/rbac/rbac.schema.js"
export type {
	Role,
	CreateRoleInput,
	UpdateRoleRequest,
	SetRolePermissionsRequest,
	PermissionCatalogEntry,
	UserWithRoles,
	AssignRoleRequest,
	RemoveRoleRequest,
	CreateUserRequest,
	CreateUserResponse,
	SetUserDisabledRequest,
} from "./modules/v1/rbac/rbac.schema.js"

// Corpus (ingestion) contract + schemas
export { corpusContract } from "./modules/v1/corpus/corpus.contract.js"
export {
	DocumentSchema,
	DocumentStatusSchema,
	DocumentMetadataSchema,
	IngestionJobSchema,
	IngestionJobDetailSchema,
	IngestionJobStatsSchema,
	IngestionJobStatusSchema,
	JobDocumentSchema,
} from "./modules/v1/corpus/corpus.schema.js"
export type {
	CorpusDocument,
	DocumentStatus,
	CorpusDocumentMetadata,
	IngestionJob,
	IngestionJobDetail,
	IngestionJobStats,
	IngestionJobStatus,
	JobDocument,
} from "./modules/v1/corpus/corpus.schema.js"

// Chat (generation) contract + schemas
export { chatContract } from "./modules/v1/chat/chat.contract.js"
export {
	ChatQueryInputSchema,
	ChatAnswerSchema,
	ChatCitationSchema,
	ChatTokenUsageSchema,
	ConversationSummarySchema,
	ConversationMessageSchema,
	ConversationDetailSchema,
} from "./modules/v1/chat/chat.schema.js"
export type {
	ChatQueryInput,
	ChatAnswer,
	ChatCitation,
	ChatTokenUsage,
	ConversationSummary,
	ConversationMessage,
	ConversationDetail,
} from "./modules/v1/chat/chat.schema.js"

// API keys (security, SC-3) contract + schemas
export { apiKeysContract } from "./modules/v1/apikeys/apikeys.contract.js"
export {
	ApiKeySchema,
	CreateApiKeyRequestSchema,
	CreateApiKeyResponseSchema,
} from "./modules/v1/apikeys/apikeys.schema.js"
export type {
	ApiKey,
	CreateApiKeyRequest,
	CreateApiKeyResponse,
} from "./modules/v1/apikeys/apikeys.schema.js"

// Golden evaluation set (evaluation, EV-1) contract + schemas
export { evalContract } from "./modules/v1/eval/eval.contract.js"
export {
	GoldenItemSchema,
	GoldenItemStatusSchema,
	CreateGoldenItemRequestSchema,
	UpdateGoldenItemRequestSchema,
	GoldenSetExportSchema,
	GoldenChunkSearchRequestSchema,
	GoldenChunkCandidateSchema,
} from "./modules/v1/eval/eval.schema.js"
export type {
	GoldenItem,
	GoldenItemStatus,
	CreateGoldenItemRequest,
	UpdateGoldenItemRequest,
	GoldenSetExport,
	GoldenChunkSearchRequest,
	GoldenChunkCandidate,
} from "./modules/v1/eval/eval.schema.js"

// Current-user access schemas
export { meContract } from "./modules/v1/me/me.contract.js"
export { MePermissionsSchema } from "./modules/v1/me/me.schema.js"
export type { MePermissions } from "./modules/v1/me/me.schema.js"

// Future versions:
// export { v2Contract, type V2Contract } from "./modules/v2/v2.contract.js"
// export const v2 = { ... }
// export type { Todo as V2Todo, ... } from "./modules/v2/..."
