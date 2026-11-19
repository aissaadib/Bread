export type MediaInfo = {
  path: string;
  name: string;
  durationSeconds: number;
  sizeBytes: number;
  width: number | null;
  height: number | null;
  displayWidth: number | null;
  displayHeight: number | null;
  rotation: number;
  frameRate: string | null;
  videoCodec: string | null;
  audioCodec: string | null;
  audioTracks: number;
};

export type Candidate = {
  id?: number | null;
  startMs: number;
  peakMs: number;
  endMs: number;
  score: number;
  signals: string[];
  explanation: string;
  title: string;
  titleSource: string;
  clipPath: string | null;
  origin?: string;
  aiTitle?: string | null;
  aiEventType?: string | null;
  aiReason?: string | null;
  aiScore?: number | null;
  aiPeakMs?: number | null;
  aiFocusX?: number | null;
  aiFocusY?: number | null;
  combinedScore?: number | null;
};

export type HistoryEntry = {
  id: string;
  sourceName: string;
  path: string;
  status: string;
  stage: string;
  progress: number;
  createdAt: string;
  errorMessage: string | null;
  notices: string[];
  aiStatus?: string;
  aiError?: string | null;
  aiModel?: string | null;
  aiWindows?: number;
  aiMessage?: string | null;
  candidates: Candidate[];
};

export type CaptionWord = {
  word: string;
  start_seconds: number;
  end_seconds: number;
};

export type CaptionSegment = {
  start_seconds: number;
  end_seconds: number;
  text: string;
  language?: string;
  words?: CaptionWord[];
};

export type CaptionData = {
  available: boolean;
  hasTranscript: boolean;
  segments: CaptionSegment[];
};

export type FramingAspect = "vertical" | "square" | "original";
export type FramingMode = "full_frame" | "crop" | "original";

export type StudioClip = {
  key: string;
  candidateId: number | null;
  title: string;
  peakMs: number;
  startMs: number;
  endMs: number;
  detectedStartMs: number;
  detectedEndMs: number;
  aiFocusX?: number | null;
  aiFocusY?: number | null;
  aiEventType?: string | null;
};

export type ExportRecord = {
  id: string;
  batchId: string;
  jobId: string;
  candidateId: number | null;
  sourcePath: string;
  title: string;
  startMs: number;
  endMs: number;
  peakMs: number;
  aspect: FramingAspect;
  mode: FramingMode;
  cropX: number;
  cropY: number;
  captions: boolean;
  crf: number;
  outputPath: string | null;
  status: string;
  stage: string;
  progress: number;
  errorMessage: string | null;
  createdAt: string;
  updatedAt: string;
};

export type ExportEvent = {
  type: string;
  batchId?: string;
  itemId?: string;
  outputPath?: string;
  stage?: string;
  progress?: number;
  overallProgress?: number | null;
  message?: string;
  cancelled?: boolean;
  completed?: number;
  failed?: number;
  total?: number;
};

export type StartExportResult = {
  batchId: string;
  exports: ExportRecord[];
};

export type AiSettings = {
  enabled: boolean;
  model: string;
  maxWindows: number;
  consentAccepted: boolean;
  hasKey: boolean;
  status: "disabled" | "missing_key" | "ready" | "running" | string;
  supportedModels: string[];
  privacyNote: string;
};

export type AiConnectionResult = {
  ok: boolean;
  model: string;
  message: string;
  latencyMs: number | null;
};

export type AiAssessmentItem = {
  candidateId: number | null;
  windowStartMs: number;
  windowEndMs: number;
  origin: string;
  isHighlight: boolean;
  eventType: string;
  interestScore: number;
  peakMs: number | null;
  reason: string;
  aiTitle: string | null;
  focusX: number | null;
  focusY: number | null;
  mostlyMenu: boolean | null;
  combinedScore: number;
  model: string;
  rejected?: boolean;
  code?: string;
  message?: string;
};

export type AiEvent = {
  type: string;
  jobId?: string;
  total?: number;
  assessed?: number;
  failed?: number;
  rejected?: number;
  model?: string;
  message?: string | null;
  rateLimited?: boolean;
  cancelled?: boolean;
  stage?: string;
  progress?: number;
  item?: AiAssessmentItem;
};
