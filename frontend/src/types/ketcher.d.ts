export {};

declare global {
  interface Window {
    ketcher?: {
      getSmiles: () => Promise<string>;
      setMolecule?: (molecule: string) => Promise<void>;
      clear?: () => Promise<void>;
    };
  }
}
