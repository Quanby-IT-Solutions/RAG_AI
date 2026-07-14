import { ValidationPipe, VersioningType, type INestApplication } from "@nestjs/common"
import { Test, type TestingModule } from "@nestjs/testing"
import request from "supertest"
import { type App } from "supertest/types"

import { AppModule } from "@/app.module"
import { db } from "@/common/database/database.client"

const API_PREFIX = "api"
const API_VERSION = "1"
const HEALTH_ENDPOINT = `/api/health`

describe("Health (e2e)", () => {
	let app: INestApplication<App>

	const createTestApp = async (): Promise<INestApplication<App>> => {
		const moduleFixture: TestingModule = await Test.createTestingModule({
			imports: [AppModule],
		})
			.overrideProvider(db)
			.useValue({
				execute: jest.fn(async () => [] as any[]),
			})
			.compile()

		const nestApp = moduleFixture.createNestApplication()
		nestApp.setGlobalPrefix(API_PREFIX)
		nestApp.enableVersioning({
			type: VersioningType.URI,
			defaultVersion: API_VERSION,
		})
		nestApp.useGlobalPipes(
			new ValidationPipe({
				whitelist: true,
				forbidNonWhitelisted: true,
				transform: true,
			})
		)
		await nestApp.init()
		return nestApp
	}

	const getHealth = () => request(app.getHttpServer()).get(HEALTH_ENDPOINT)

	beforeEach(async () => {
		app = await createTestApp()
	})

	afterEach(async () => {
		await app.close()
	})

	it("returns health status with all checks", async () => {
		const response = await getHealth().expect(200)

		expect(response.body).toMatchObject({
			status: "ok",
			checks: {
				database: { status: "up" },
				cache: { status: "not_configured" },
			},
		})
		expect(response.body.uptime).toBeDefined()
		expect(typeof response.body.uptime).toBe("number")
		expect(response.body.timestamp).toBeDefined()
		expect(response.body.version).toBeDefined()
	})

	it("database check is up", async () => {
		const response = await getHealth().expect(200)
		expect(response.body.checks.database.status).toBe("up")
	})

	it("cache check is not configured", async () => {
		const response = await getHealth().expect(200)
		expect(response.body.checks.cache.status).toBe("not_configured")
	})
})
