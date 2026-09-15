/** Shared by the HTML bootstrap and business routing; no UI dependencies. */
export const canvasRoutePaths = {
  structureWorkbench: '/structure-workbench',
  homopolymerPrediction: '/homopolymer-property-prediction',
  explorer: '/explorer',
  databaseQuery: '/database-query',
  conditionalGeneration: '/conditional-generation',
  reverseDesign: '/reverse-design'
} as const;
