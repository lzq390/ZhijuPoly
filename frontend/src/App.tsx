import { KetcherEditor } from "./components/KetcherEditor";
import { Layout } from "./components/Layout";
import { QueryPanel } from "./components/QueryPanel";
import { ResultsDisplay } from "./components/ResultsDisplay";
import { StructurePreview3D } from "./components/StructurePreview3D";
import { useKetcher } from "./hooks/useKetcher";
import { useQuery } from "./hooks/useQuery";

export default function App() {
  const { smiles, setSmiles, iframeRef, setIsReady } = useKetcher("*CC*");
  const { request, setRequest, isLoading, error, data, submit } = useQuery();

  return (
    <Layout>
      <div className="space-y-6">
        <div className="grid items-stretch gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
          <KetcherEditor
            smiles={smiles}
            iframeRef={iframeRef}
            onReadyChange={setIsReady}
            onChange={(value) => {
              setSmiles(value);
              setRequest({ ...request, smiles: value });
            }}
          />
          <div className="flex h-full flex-col gap-6">
            <StructurePreview3D smiles={smiles} />
            <QueryPanel
              className="flex-1"
              request={{ ...request, smiles }}
              onChange={setRequest}
              onSubmit={() => submit({ ...request, smiles })}
              disabled={isLoading || smiles.trim().length === 0}
            />
          </div>
        </div>
        <ResultsDisplay data={data} error={error} />
      </div>
    </Layout>
  );
}
