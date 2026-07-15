import { Module } from "@nestjs/common"

import { ApiKeysV1Module } from "./apikeys/apikeys.module"
import { ChatModule } from "./chat/chat.module"
import { CorpusModule } from "./corpus/corpus.module"
import { EvalModule } from "./eval/eval.module"
import { ExamplesModule } from "./examples/examples.module"
import { HealthModule } from "./health/health.module"
import { MeModule } from "./me/me.module"
import { RbacAdminModule } from "./rbac/rbac.module"
import { TicketsModule } from "./tickets/tickets.module"

@Module({
	imports: [
		ApiKeysV1Module,
		ChatModule,
		CorpusModule,
		EvalModule,
		ExamplesModule,
		HealthModule,
		MeModule,
		RbacAdminModule,
		TicketsModule,
	],
})
export class V1Module {}
