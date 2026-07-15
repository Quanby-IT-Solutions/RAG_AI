import { Global, Module } from "@nestjs/common"

import { AuditModule } from "@/common/audit/audit.module"
import { RbacModule } from "@/common/rbac/rbac.module"

import { ApiKeysService } from "./apikeys.service"

/** Global: the RbacGuard (APP_GUARD) needs ApiKeysService everywhere. */
@Global()
@Module({
	imports: [AuditModule, RbacModule],
	providers: [ApiKeysService],
	exports: [ApiKeysService],
})
export class ApiKeysModule {}
