import { Global, Module } from "@nestjs/common"

import { AuditService } from "./audit.service"

/**
 * Global: the audit spine is cross-cutting (RBAC guard denials, module state
 * changes) and must be injectable everywhere without per-module imports.
 */
@Global()
@Module({
	providers: [AuditService],
	exports: [AuditService],
})
export class AuditModule {}
