package team

import (
	"strings"
)

func capabilityGapCoachingBlock() string {
	return strings.Join([]string{
		"Capability-gap rule: if the work is blocked because the needed specialist, channel, skill, or tool path does not exist yet, treat that gap as the next real work item.",
		"Do not fall back to a review bundle, proof packet, artifact shell, or local substitute deliverable when the missing capability is what is actually preventing execution.",
		"Concrete sequence: create the missing specialist with team_member first; if the work will span more than one turn, create the missing execution channel with team_channel; propose or update the missing skill block in the same turn; and if the blocker is a tool or provider gap, open a tool-discovery/research lane named for the exact tool you need so the office can discover, validate, and enable it.",
		"Example: if the work needs video generation and you do not already have a usable path, create a discovery lane for Remotion or the exact video tool before drafting any deliverable shell.",
	}, "\n")
}

func taskHygieneCoachingBlock() string {
	return strings.Join([]string{
		"Task hygiene rule: if a live business lane gets named or reframed as a review packet, proof artifact, blueprint-derived scaffold, rubric, or other internal shell, rewrite that lane in the same turn.",
		"Replace it with either the next real deliverable/customer-facing/business-facing step or the exact capability-enablement task that unblocks that step.",
	}, "\n")
}
