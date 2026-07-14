import { ValidationPipe, VersioningType, type INestApplication } from "@nestjs/common"
import { Test, type TestingModule } from "@nestjs/testing"
import request from "supertest"
import { type App } from "supertest/types"

import { AppModule } from "../src/app.module"

describe("Examples API (e2e)", () => {
	let app: INestApplication<App>

	beforeEach(async () => {
		const moduleFixture: TestingModule = await Test.createTestingModule({
			imports: [AppModule],
		}).compile()

		app = moduleFixture.createNestApplication()
		app.setGlobalPrefix("api")
		app.enableVersioning({
			type: VersioningType.URI,
			defaultVersion: "1",
		})
		app.useGlobalPipes(
			new ValidationPipe({
				whitelist: true,
				forbidNonWhitelisted: true,
				transform: true,
			})
		)
		await app.init()
	})

	it("/api/v1/examples/todos (GET)", async () => {
		const response = await request(app.getHttpServer()).get("/api/v1/examples/todos").expect(200)
		expect(response.body).toEqual(
			expect.arrayContaining([
				expect.objectContaining({ title: "First todo example" }),
				expect.objectContaining({ title: "Second todo example" }),
			])
		)
	})

	it("/api/v1/examples/todos (GET) returns versioned payload", async () => {
		const response = await request(app.getHttpServer()).get("/api/v1/examples/todos").expect(200)
		expect(response.body).toEqual(
			expect.arrayContaining([
				expect.objectContaining({ title: "First todo example", apiVersion: "1" }),
				expect.objectContaining({ title: "Second todo example", apiVersion: "1" }),
			])
		)
	})

	it("/api/v1/examples/todos (POST/PUT/PATCH/DELETE) full CRUD", async () => {
		const server = app.getHttpServer()

		// Create
		const created = await request(server)
			.post("/api/v1/examples/todos")
			.send({ title: "New todo", completed: false })
			.expect(201)
		expect(created.body).toEqual(expect.objectContaining({ title: "New todo", completed: false }))

		const id = created.body.id

		// Replace
		const replaced = await request(server)
			.put(`/api/v1/examples/todos/${id}`)
			.send({ title: "Replaced todo", completed: true })
			.expect(200)
		expect(replaced.body).toEqual(
			expect.objectContaining({ id, title: "Replaced todo", completed: true })
		)

		// Update
		const patched = await request(server)
			.patch(`/api/v1/examples/todos/${id}`)
			.send({ completed: false })
			.expect(200)
		expect(patched.body).toEqual(
			expect.objectContaining({ id, title: "Replaced todo", completed: false })
		)

		// Delete
		await request(server).delete(`/api/v1/examples/todos/${id}`).expect(200)
	})
})
