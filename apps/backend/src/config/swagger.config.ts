import { Logger, type INestApplication } from "@nestjs/common"
import { OpenAPIGenerator } from "@orpc/openapi"
import { ZodToJsonSchemaConverter } from "@orpc/zod/zod4"
import { apiReference } from "@scalar/nestjs-api-reference"

import { API_VERSIONS, getVersionKeys, type VersionKey } from "@/config/api-versions.config"
import { mergeBetterAuthSchema } from "@/utils/openapi"

const logger = new Logger("SwaggerConfig")

/**
 * Generate an OpenAPI document from the versioned oRPC contract
 *
 * Uses oRPC's native OpenAPIGenerator to produce a spec directly from contracts.
 * This ensures paths, summaries, descriptions, tags, and response schemas are all
 * derived from the contract definitions — no NestJS route scanning needed.
 */
async function generateOpenAPIDocument(version: VersionKey) {
	const generator = new OpenAPIGenerator({
		schemaConverters: [new ZodToJsonSchemaConverter()],
	})

	const versionContract = API_VERSIONS[version].contract

	return generator.generate(versionContract, {
		info: {
			title: `API Documentation ${version}`,
			description: `Type-safe API with auto-generated documentation for version ${version}`,
			version,
		},
		servers: [{ url: `/api` }],
		security: [{ cookieAuth: [] }],
		components: {
			securitySchemes: {
				cookieAuth: {
					type: "apiKey",
					in: "cookie",
					name: "better-auth.session_token",
				},
			},
		},
	})
}

/**
 * Set up Scalar docs for a specific version
 */
async function setupVersionedDocs(app: INestApplication, version: VersionKey): Promise<void> {
	const baseDoc = await generateOpenAPIDocument(version)
	const mergedDoc = await mergeBetterAuthSchema(baseDoc, version)
	const basePath = `api/${version}`

	// Serve the OpenAPI spec as JSON
	app.getHttpAdapter().get(`/${basePath}/spec.json`, (_req: unknown, res: any) => {
		res.json(mergedDoc)
	})

	// Serve Scalar API reference
	app.use(`/${basePath}/docs`, apiReference({ url: `/${basePath}/spec.json`, theme: "none" }))

	logger.log(`Documentation available at /${basePath}/spec.json and /${basePath}/docs`)
}

/**
 * Set up documentation for all registered API versions
 */
export async function setupSwagger(app: INestApplication): Promise<void> {
	const versions = getVersionKeys()

	if (versions.length === 0) {
		logger.warn("No API versions found, skipping documentation setup")
		return
	}

	logger.log(`Setting up documentation for versions: ${versions.join(", ")}`)

	for (const version of versions) {
		await setupVersionedDocs(app, version)
	}
}
