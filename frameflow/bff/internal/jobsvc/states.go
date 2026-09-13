package jobsvc

import "errors"

// ErrIllegalTransition is returned by Advance when the trigger is not
// allowed from the current state.
var ErrIllegalTransition = errors.New("jobsvc: illegal state transition")

// Advance computes the next state given current + a job_type trigger.
//
// MVP rule: each trigger has a direct legal transition:
//
//	CREATED              --storyboard--> STORYBOARD_READY
//	  (skipping PLANNING is allowed in MVP)
//	STORYBOARD_READY     --animatic----> ANIMATIC_RENDERING
//	ANIMATIC_RENDERING   --animatic_done--> ANIMATIC_READY
//	ANIMATIC_READY       --sample------> SAMPLE_RENDERING
//	SAMPLE_RENDERING     --sample_done--> SAMPLE_READY
//	SAMPLE_READY         --approve----> WAITING_APPROVAL
//	WAITING_APPROVAL     --render------>  FINAL_RENDERING
//	FINAL_RENDERING      --render_done--> COMPLETED
//
// Cancel transitions from any non-terminal state to CANCELLED.
// Failed from any non-terminal via SetError (not modeled here — SetError
// writes FAILED directly).
//
// Returns ErrIllegalTransition if the trigger is not allowed from the
// current state. The returned (next, nil) is always the destination state;
// (current, ErrIllegalTransition) signals a reject — caller can decide to
// 409 or ignore.
func Advance(current, trigger string) (string, error) {
	switch trigger {
	case "storyboard":
		if current == StatusCreated || current == StatusPlanning {
			return StatusStoryboardReady, nil
		}
	case "animatic":
		if current == StatusStoryboardReady {
			return StatusAnimaticRendering, nil
		}
	case "animatic_done":
		if current == StatusAnimaticRendering {
			return StatusAnimaticReady, nil
		}
	case "sample":
		if current == StatusAnimaticReady {
			return StatusSampleRendering, nil
		}
	case "sample_done":
		if current == StatusSampleRendering {
			return StatusSampleReady, nil
		}
	case "approve":
		// Phase 7: explicit user approval is required after sample
		// completes and before render starts. Idempotent — calling
		// approve from WAITING_APPROVAL returns the same state.
		if current == StatusSampleReady {
			return StatusWaitingApproval, nil
		}
		if current == StatusWaitingApproval {
			return StatusWaitingApproval, nil
		}
	case "render":
		if current == StatusSampleReady || current == StatusWaitingApproval {
			return StatusFinalRendering, nil
		}
	case "render_done":
		if current == StatusFinalRendering {
			return StatusCompleted, nil
		}
	case "cancel":
		if !isTerminal(current) {
			return StatusCancelled, nil
		}
	}
	return current, ErrIllegalTransition
}

// IsTerminal returns true if the status cannot transition further.
// Used by Advance("cancel", ...) and by the runner to short-circuit.
func IsTerminal(s string) bool {
	return isTerminal(s)
}

func isTerminal(s string) bool {
	return s == StatusCompleted || s == StatusFailed || s == StatusCancelled
}
