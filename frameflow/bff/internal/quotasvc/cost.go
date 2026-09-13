package quotasvc

// JobType constants — keep aligned with production_jobs.job_type values.
const (
	JobTypeStoryboard = "storyboard"
	JobTypeAnimatic   = "animatic"
	JobTypeSample     = "sample"
	JobTypeRender     = "render"
)

// EstimateCost returns the credit cost for a job type.
// MVP table (per §17.E / tasks.yaml):
//   storyboard = 1   animatic = 5   sample = 10   render = 50
// Unknown values fall back to 1.
func EstimateCost(jobType string) float64 {
	switch jobType {
	case JobTypeStoryboard:
		return 1
	case JobTypeAnimatic:
		return 5
	case JobTypeSample:
		return 10
	case JobTypeRender:
		return 50
	}
	return 1
}
