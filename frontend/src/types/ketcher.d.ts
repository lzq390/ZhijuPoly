export {};

declare global {
  interface Window {
    ketcher?: {
      getSmiles: () => Promise<string>;
      setMolecule?: (molecule: string) => Promise<void>;
      clear?: () => Promise<void>;
    };
    $3Dmol?: {
      createViewer: (
        element: HTMLElement,
        config?: Record<string, unknown>,
        sharedConfig?: Record<string, unknown>
      ) => {
        addModel: (data: string, format: string) => void;
        setStyle: (selector: Record<string, unknown>, style: Record<string, unknown>) => void;
        zoomTo: () => void;
        render: () => void;
        clear: () => void;
        setBackgroundColor: (color: string) => void;
      };
    };
  }
}
