import { Module } from "@nestjs/common"

import { ChatModule } from "./chat/chat.module"
import { CorpusModule } from "./corpus/corpus.module"
import { ExamplesModule } from "./examples/examples.module"
import { HealthModule } from "./health/health.module"
import { MeModule } from "./me/me.module"
import { RbacAdminModule } from "./rbac/rbac.module"
import { TicketsModule } from "./tickets/tickets.module"

@Module({
	imports: [
		ChatModule,
		CorpusModule,
		ExamplesModule,
		HealthModule,
		MeModule,
		RbacAdminModule,
		TicketsModule,
	],
})
export class V1Module {}
