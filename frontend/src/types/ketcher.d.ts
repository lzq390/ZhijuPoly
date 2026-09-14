export {};

declare global {
  interface Window {
    $3Dmol?: {
      createViewer: (
        element: HTMLElement,
        config?: Record<string, unknown>,
        sharedConfig?: Record<string, unknown>
      ) => {
        addModel: (data: string, format: string) => void;
        setStyle: (selector: Record<string, unknown>, style: Record<string, unknown>) => void;
        addStyle?: (selector: Record<string, unknown>, style: Record<string, unknown>) => void;
        addArrow?: (spec: {
          start: { x: number; y: number; z: number };
          end: { x: number; y: number; z: number };
          radius?: number;
          radiusRatio?: number;
          mid?: number;
          color?: string;
        }) => void;
        removeAllShapes?: () => void;
        zoomTo: () => void;
        render: () => void;
        clear: () => void;
        resize?: () => void;
        setBackgroundColor: (color: string) => void;
      };
    };
  }
}
