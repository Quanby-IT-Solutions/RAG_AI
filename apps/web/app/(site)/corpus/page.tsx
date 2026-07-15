import type { Metadata } from "next"

import { CorpusView } from "@/features/corpus/components/corpus-view"

export const metadata: Metadata = {
	title: "Corpus",
}

export default function CorpusPage() {
	return <CorpusView />
}
