import { Config } from "@remotion/cli/config";

// Faster JPEG encoding for image-heavy compositions.
Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);
// Keep concurrency modest for stability on weaker machines.
Config.setConcurrency(1);
