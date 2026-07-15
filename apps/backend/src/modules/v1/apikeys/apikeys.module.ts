import { Module } from "@nestjs/common"

import { RbacModule } from "@/common/rbac/rbac.module"

import { ApiKeysController } from "./apikeys.controller"

/** ApiKeysService is provided by the GLOBAL common ApiKeysModule. */
@Module({
	imports: [RbacModule],
	controllers: [ApiKeysController],
})
export class ApiKeysV1Module {}
