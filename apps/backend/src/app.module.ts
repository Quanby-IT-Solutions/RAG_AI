import { Module, RequestMethod } from "@nestjs/common"
import { ConfigModule } from "@nestjs/config"
import { APP_FILTER, APP_GUARD, APP_INTERCEPTOR, APP_PIPE } from "@nestjs/core"
import { ThrottlerModule } from "@nestjs/throttler"
import { ThrottlerStorageRedisService } from "@nest-lab/throttler-storage-redis"
import { SentryModule } from "@sentry/nestjs/setup"
import { AuthGuard, AuthModule } from "@thallesp/nestjs-better-auth"
import { LoggerModule } from "nestjs-pino"
import { ZodSerializerInterceptor, ZodValidationPipe } from "nestjs-zod"

import { getAuth } from "@repo/auth"

import { ApiKeysModule } from "@/common/apikeys/apikeys.module"
import { HttpExceptionFilter } from "@/common/filters/http-exception.filter"
import { SentryExceptionFilter } from "@/common/filters/sentry-exception.filter"
import { buildPinoHttpOptions } from "@/config/pino-logger.config"
import { V1Module } from "@/modules/v1/v1.module"
import { skipStrictThrottle } from "@/shared/decorators/strict-throttle.decorator"
import { ApiKeyAuthGuard } from "@/shared/guards/api-key-auth.guard"
import { RbacGuard } from "@/shared/guards/rbac.guard"
import { ThrottlerProxyGuard } from "@/shared/guards/throttler-proxy.guard"
import { TelemetryLifecycle } from "@/telemetry/telemetry.lifecycle"

import { AnalyticsModule } from "./common/analytics/analytics.module"
import { AuditModule } from "./common/audit/audit.module"
import { MaintenanceModule } from "./common/maintenance/maintenance.module"
import { ORPCCommonModule } from "./common/orpc/orpc.module"
import { RbacModule } from "./common/rbac/rbac.module"
import { RedisModule } from "./common/redis/redis.module"
import { REDIS_CLIENT, type RedisClient } from "./common/redis/redis.provider"
import { ResilientThrottlerStorage } from "./common/redis/resilient-throttler.storage"
import { env } from "./config/env.config"

@Module({
	imports: [
		// Sentry request/span instrumentation for Nest (controllers, guards,
		// interceptors, pipes). The SDK itself is initialised much earlier, in
		// `src/instrument.ts` — this only wires the Nest-specific tracing. It is a
		// no-op when `Sentry.init` never ran.
		SentryModule.forRoot(),
		// Core configuration
		ConfigModule.forRoot({
			isGlobal: true,
			envFilePath: ".env",
			load: [() => env],
		}),
		// RAG API-key authentication (x-api-key). Registered first so its service
		// is available to the ApiKeyAuthGuard in the global guard chain.
		ApiKeysModule,
		// Structured logging (provides the injectable pino-backed Logger).
		// autoLogging is disabled here so this LoggerModule pino-http middleware
		// emits NO request/response log lines: the standalone pino-http middleware
		// registered before Better Auth (bootstrap.ts) is the authoritative
		// request/response logger for all routes. This middleware still runs, but
		// its genReqId reuses the `req.id` already assigned by the pre-auth
		// middleware (see buildPinoHttpOptions), so both agree on one correlation
		// id and the response header is never overwritten with a fresh one.
		//
		// forRoutes is set explicitly. nestjs-pino's default is `{ path: "*" }`.
		// Under the global `api` prefix, Nest 11 rewrites that legacy wildcard
		// and logs a LegacyRouteConverter warning. `{*path}` is the v8 form Nest
		// rewrites `*` into, so coverage stays the same and the warning is not
		// emitted.
		LoggerModule.forRoot({
			pinoHttp: buildPinoHttpOptions({ autoLogging: false }),
			forRoutes: [{ path: "{*path}", method: RequestMethod.ALL }],
		}),
		// Rate limiting.
		//
		// Counters live in Redis when REDIS_URL is set, so a limit of N is N
		// across the whole cluster rather than N per instance. Without it the
		// module falls back to its in-memory store: correct for a single
		// instance, and the startup warning in redis.provider.ts says so.
		//
		// Degradation is deliberately FAIL-OPEN: if Redis goes away, requests
		// are allowed rather than refused. The throttler is a supporting abuse
		// control, and turning a Redis outage into a total outage trades a
		// small risk for a large one. This is the opposite of the RBAC cache's
		// fail-closed choice, because that one guards correctness.
		ThrottlerModule.forRootAsync({
			inject: [REDIS_CLIENT],
			useFactory: (redis: RedisClient) => ({
				throttlers: [
					{
						name: "default",
						ttl: env.THROTTLE_TTL,
						limit: env.THROTTLE_LIMIT,
					},
					{
						name: "strict",
						ttl: env.THROTTLE_STRICT_TTL,
						limit: env.THROTTLE_STRICT_LIMIT,
						// Opt-in only: skipped for every route except handlers
						// marked with @StrictThrottle(). Keeps reads on `default`.
						skipIf: skipStrictThrottle,
					},
				],
				...(redis
					? { storage: new ResilientThrottlerStorage(new ThrottlerStorageRedisService(redis)) }
					: {}),
			}),
		}),
		// Authentication (controllers disabled - we register versioned routes in
		// setupBetterAuth). The global AuthGuard is disabled so the guard chain
		// below is deterministic: Throttler → ApiKeyAuthGuard → RbacGuard (SC-3).
		AuthModule.forRoot({
			auth: getAuth(),
			disableControllers: true,
			disableGlobalAuthGuard: true,
		}),
		// oRPC setup
		ORPCCommonModule,
		// Shared Redis connection for the throttler and the RBAC cache.
		RedisModule,
		// PostHog: one shared client, or none when analytics is off.
		AnalyticsModule,
		// Append-only audit spine (global). The audit trail outgrows RBAC.
		AuditModule,
		// RBAC services
		RbacModule,
		// Scheduled sweeps of expired rows (idempotency replay cache; verifications).
		MaintenanceModule,
		// Versioned modules
		V1Module,
	],
	providers: [
		// Flushes OpenTelemetry spans and metrics on shutdown (a no-op when off).
		TelemetryLifecycle,
		// Global providers
		{
			provide: APP_PIPE,
			useClass: ZodValidationPipe,
		},
		{
			provide: APP_INTERCEPTOR,
			useClass: ZodSerializerInterceptor,
		},
		// Two filters, one contract. Nest routes an exception to the most specific
		// matching filter, so the split is by exception type, not by ordering luck:
		//   - HttpException (deliberate, typed)  -> HttpExceptionFilter, which
		//     renders the real status/message and captures only 5xx.
		//   - anything else (bugs, driver faults) -> SentryExceptionFilter, which
		//     captures it and renders a generic 500 in the same shape. A client
		//     request-parsing error is returned as that 4xx and is not captured.
		// Neither can see the other's exceptions, so nothing is reported twice.
		// The catch-all is registered first, per Sentry's Nest setup guidance.
		{
			provide: APP_FILTER,
			useClass: SentryExceptionFilter,
		},
		{
			provide: APP_FILTER,
			useClass: HttpExceptionFilter,
		},
		{
			provide: APP_GUARD,
			useClass: ThrottlerProxyGuard,
		},
		// Authentication: session (library AuthGuard, provided here so Nest can
		// build it for ApiKeyAuthGuard to delegate to) or x-api-key (SC-3). Must
		// precede RbacGuard.
		AuthGuard,
		{
			provide: APP_GUARD,
			useClass: ApiKeyAuthGuard,
		},
		{
			provide: APP_GUARD,
			useClass: RbacGuard,
		},
	],
})
export class AppModule {}
