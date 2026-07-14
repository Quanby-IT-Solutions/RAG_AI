import { applyDecorators, type Type } from "@nestjs/common"
import { ApiOperation, ApiResponse, type ApiOperationOptions } from "@nestjs/swagger"

interface ApiEndpointOptions {
	/** Short summary of what the endpoint does */
	summary: string
	/** Detailed description (optional) */
	description?: string
	/** Success response DTO class */
	response?: Type<unknown>
	/** Success status code (default: 200) */
	status?: number
}

/**
 * Combines @ApiOperation and @ApiResponse into a single decorator
 * Provides full manual control over API documentation
 *
 * @example
 * ```typescript
 * @ApiEndpoint({
 *   summary: "Retrieve all todos",
 *   description: "Returns a list of all todos for the authenticated user",
 *   response: TodoListDto,
 * })
 * ```
 */
export function ApiEndpoint(options: ApiEndpointOptions) {
	const decorators: Array<ClassDecorator | MethodDecorator | PropertyDecorator> = []

	// Add operation summary and description
	const operationOptions: ApiOperationOptions = {
		summary: options.summary,
	}
	if (options.description) {
		operationOptions.description = options.description
	}
	decorators.push(ApiOperation(operationOptions))

	// Add success response
	if (options.response) {
		const status = options.status ?? 200
		decorators.push(
			ApiResponse({
				status,
				type: options.response,
				description: "Success",
			})
		)
	}

	return applyDecorators(...decorators)
}
