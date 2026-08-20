import {
  AbsoluteFill,
  Sequence,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

// Word-level caption for TikTok-style highlight display.
// Shared shape with `CaptionOverlay` so the bilingual component is a strict
// superset (it accepts two parallel arrays of WordCaption).
export interface WordCaption {
  word: string;
  startMs: number;
  endMs: number;
}

interface BilingualCaptionOverlayProps {
  /** Master timeline — English (or any primary) language. Drives the
   *  page boundaries and per-row highlight timing. */
  primaryWords: WordCaption[];
  /** Secondary language — Chinese (or any CJK-capable). Each row's
   *  per-word highlight uses this array's startMs/endMs directly, so the
   *  subtitle_gen contract `target_segments` (1:1 alignment with the
   *  primary) makes this work without any re-alignment pass. */
  secondaryWords: WordCaption[];

  /** How many words to show at once in a "page". */
  wordsPerPage?: number;
  /** Vertical gap (px) between primary and secondary row. */
  rowGap?: number;

  primaryFontSize?: number;
  secondaryFontSize?: number;
  primaryColor?: string;
  primaryHighlightColor?: string;
  secondaryColor?: string;
  secondaryHighlightColor?: string;
  backgroundColor?: string;

  primaryFontFamily?: string;
  secondaryFontFamily?: string;

  // Index signature so the composition satisfies Remotion's
  // `Record<string, unknown>` props constraint.
  [key: string]: unknown;
}

interface BilingualCaptionPage {
  primaryWords: WordCaption[];
  secondaryWords: WordCaption[];
  startMs: number;
  endMs: number;
}

/**
 * Slice secondary words by time-range overlap with each primary page.
 *
 * Word timestamps survive 1:1 through `nllb_translator` (it only rewrites
 * `text`), so the secondary's startMs/endMs should land on the same
 * timeline as the primary. We slice by `[pageStartMs, pageEndMs]` to
 * pick the matching subset — this keeps the rows in lockstep even if
 * one language splits a cue slightly differently.
 */
function sliceSecondaryByPage(
  primaryWords: WordCaption[],
  secondaryWords: WordCaption[],
  wordsPerPage: number,
): BilingualCaptionPage[] {
  const pages: BilingualCaptionPage[] = [];
  for (let i = 0; i < primaryWords.length; i += wordsPerPage) {
    const pagePrim = primaryWords.slice(i, i + wordsPerPage).filter((w) => w.word);
    if (pagePrim.length === 0) continue;
    const startMs = pagePrim[0].startMs;
    const endMs = pagePrim[pagePrim.length - 1].endMs;
    const pageSec = secondaryWords.filter(
      (w) => w.endMs > startMs && w.startMs < endMs,
    );
    pages.push({
      primaryWords: pagePrim,
      secondaryWords: pageSec,
      startMs,
      endMs,
    });
  }
  return pages;
}

const RowRenderer: React.FC<{
  words: WordCaption[];
  fontSize: number;
  color: string;
  highlightColor: string;
  fontFamily: string;
  currentMs: number;
}> = ({ words, fontSize, color, highlightColor, fontFamily, currentMs }) => (
  <div
    style={{
      fontSize,
      fontWeight: 700,
      fontFamily,
      lineHeight: 1.3,
      whiteSpace: "pre-wrap",
      textAlign: "center",
    }}
  >
    {words.map((w, i) => {
      const isActive = w.startMs <= currentMs && w.endMs > currentMs;
      const isPast = w.endMs <= currentMs;
      return (
        <span
          key={`${w.startMs}-${i}`}
          style={{
            color: isActive ? highlightColor : isPast ? color : `${color}99`,
            transition: "none", // CSS transitions forbidden in Remotion
            textShadow: isActive
              ? `0 0 20px ${highlightColor}66, 0 2px 4px rgba(0,0,0,0.5)`
              : "0 2px 4px rgba(0,0,0,0.5)",
            marginRight: 6,
          }}
        >
          {w.word}
        </span>
      );
    })}
  </div>
);

const PageRenderer: React.FC<
  BilingualCaptionPage & {
    primaryFontSize: number;
    secondaryFontSize: number;
    primaryColor: string;
    primaryHighlightColor: string;
    secondaryColor: string;
    secondaryHighlightColor: string;
    backgroundColor: string;
    primaryFontFamily: string;
    secondaryFontFamily: string;
    rowGap: number;
  }
> = (props) => {
  const {
    primaryWords, secondaryWords, startMs, endMs,
    primaryFontSize, secondaryFontSize,
    primaryColor, primaryHighlightColor,
    secondaryColor, secondaryHighlightColor,
    backgroundColor, primaryFontFamily, secondaryFontFamily,
    rowGap,
  } = props;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const currentMs = startMs + (frame / fps) * 1000;

  // Spring entrance — same shape as CaptionOverlay for visual consistency.
  const entrance = spring({
    frame,
    fps,
    config: { damping: 18, stiffness: 120 },
  });

  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: 80,
      }}
    >
      <div
        style={{
          opacity: entrance,
          transform: `translateY(${interpolate(entrance, [0, 1], [20, 0])}px)`,
          backgroundColor,
          borderRadius: 12,
          padding: "14px 28px",
          maxWidth: "85%",
          display: "flex",
          flexDirection: "column",
          gap: rowGap,
          alignItems: "center",
        }}
      >
        <RowRenderer
          words={primaryWords}
          fontSize={primaryFontSize}
          color={primaryColor}
          highlightColor={primaryHighlightColor}
          fontFamily={primaryFontFamily}
          currentMs={currentMs}
        />
        <RowRenderer
          words={secondaryWords}
          fontSize={secondaryFontSize}
          color={secondaryColor}
          highlightColor={secondaryHighlightColor}
          fontFamily={secondaryFontFamily}
          currentMs={currentMs}
        />
      </div>
    </AbsoluteFill>
  );
};

export const BilingualCaptionOverlay: React.FC<BilingualCaptionOverlayProps> = ({
  primaryWords,
  secondaryWords,
  wordsPerPage = 6,
  rowGap = 6,
  primaryFontSize = 42,
  secondaryFontSize = 36,
  primaryColor = "#F8FAFC",
  primaryHighlightColor = "#22D3EE",
  secondaryColor = "#E2E8F0",
  secondaryHighlightColor = "#FBBF24",
  backgroundColor = "rgba(15, 23, 42, 0.78)",
  primaryFontFamily = "Space Grotesk, Inter, system-ui, sans-serif",
  secondaryFontFamily = "Noto Sans CJK SC, Noto Sans SC, system-ui, sans-serif",
}) => {
  const { fps } = useVideoConfig();
  const pages = sliceSecondaryByPage(primaryWords, secondaryWords, wordsPerPage);

  return (
    <AbsoluteFill>
      {pages.map((page, i) => {
        const fromFrame = Math.round((page.startMs / 1000) * fps);
        const nextStart = pages[i + 1]?.startMs ?? page.endMs + 500;
        const duration = Math.max(
          1,
          Math.round(((nextStart - page.startMs) / 1000) * fps)
        );
        return (
          <Sequence key={i} from={fromFrame} durationInFrames={duration}>
            <PageRenderer
              {...page}
              primaryFontSize={primaryFontSize}
              secondaryFontSize={secondaryFontSize}
              primaryColor={primaryColor}
              primaryHighlightColor={primaryHighlightColor}
              secondaryColor={secondaryColor}
              secondaryHighlightColor={secondaryHighlightColor}
              backgroundColor={backgroundColor}
              primaryFontFamily={primaryFontFamily}
              secondaryFontFamily={secondaryFontFamily}
              rowGap={rowGap}
            />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};