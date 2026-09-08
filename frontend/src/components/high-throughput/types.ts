import type { HighThroughputTargetKey } from "../../constants/highThroughputDemoScenario";

export type RecommendationSelection = {
  targetKey: HighThroughputTargetKey;
  candidateId: string;
};

export type RecommendationValidationValues = Record<string, string>;

export type PriorDataUploadState = {
  targetKey: HighThroughputTargetKey;
  fileName: string;
  fileType: string;
  sampleCount: number;
  fieldCount: number;
  expectedFileName: string;
  propertyColumn: string;
  uploadedAt: string;
  uploadToken: string;
  isLoading: boolean;
  errorMessage?: string;
};

export type PriorDataUploadsState = Partial<Record<HighThroughputTargetKey, PriorDataUploadState>>;
