import { ArrowRight, BookOpen, ChevronDown, Expand, List, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import guideDocument from "../help/usage-guide.json";
import { useModalFocus } from "../hooks/useModalFocus";
import type { ActiveModule } from "../routing";
import { ModulePageHeader } from "./ModulePageHeader";
import "./UsageGuidePage.css";

type GuideScreenshot = { src: string; alt: string; caption: string };
type GuideSection = {
  id: ActiveModule;
  number: string;
  group: string;
  title: string;
  purpose: string;
  steps: string[];
  screenshots: GuideScreenshot[];
};
type GuideDocument = { title: string; introduction: string; sections: GuideSection[] };
const guide = guideDocument as GuideDocument;
const sectionId = (id: ActiveModule) => "usage-guide-" + id;

function ScreenshotPreview({ image, onClose, trigger }: {
  image: GuideScreenshot;
  onClose: () => void;
  trigger: HTMLElement;
}) {
  const scope = useRef<HTMLDivElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  useModalFocus({ active: true, open: true, scopeRef: scope, panelRef: panel, initialFocusRef: close, onClose });
  useEffect(() => {
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = overflow;
      if (trigger.isConnected) trigger.focus({ preventScroll: true });
    };
  }, [trigger]);
  return createPortal(
    <div className="np-guide-lightbox" ref={scope} onClick={event => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <div className="np-guide-lightbox__panel" role="dialog" aria-modal="true" aria-label="操作截图" ref={panel} tabIndex={-1}>
        <header><span>操作截图</span><button ref={close} type="button" aria-label="关闭截图" onClick={onClose}><X aria-hidden="true" /></button></header>
        <div className="np-guide-lightbox__image"><img src={image.src} alt={image.alt} /></div>
        <p>{image.caption}</p>
      </div>
    </div>, document.body
  );
}

function Screenshot({ image, onPreview }: {
  image: GuideScreenshot;
  onPreview: (image: GuideScreenshot, trigger: HTMLElement) => void;
}) {
  const [failed, setFailed] = useState(false);
  return <figure className="np-guide-figure">
    {failed ? <div className="np-guide-image-error" role="status">
      <p>截图暂时无法显示。</p>
      <button type="button" onClick={() => setFailed(false)}>重新加载截图</button>
    </div> : <button className="np-guide-image-button" type="button" aria-label={"放大截图：" + image.alt}
      onClick={event => onPreview(image, event.currentTarget)}>
      <img src={image.src} alt={image.alt} loading="lazy" decoding="async" onError={() => setFailed(true)} />
      <span><Expand aria-hidden="true" />点击放大</span>
    </button>}
    <figcaption>{image.caption}</figcaption>
  </figure>;
}

function GuideScreenshots({ section, onPreview }: {
  section: GuideSection;
  onPreview: (image: GuideScreenshot, trigger: HTMLElement) => void;
}) {
  const [open, setOpen] = useState(false);
  return <details className="np-guide-screenshots" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><span>操作截图 <small>{section.screenshots.length} 张</small></span><ChevronDown aria-hidden="true" /></summary>
    {open && <div className="np-guide-gallery">{section.screenshots.map(image =>
      <Screenshot key={image.src} image={image} onPreview={onPreview} />
    )}</div>}
  </details>;
}

function GuideContents({ onSelect }: { onSelect: (id: ActiveModule) => void }) {
  const groups = [...new Set(guide.sections.map(section => section.group))];
  return <div className="np-guide-contents">
    {groups.map(group => <div className="np-guide-contents__group" key={group}>
      <h3>{group}</h3>
      {guide.sections.filter(section => section.group === group).map(section =>
        <button type="button" key={section.id} onClick={() => onSelect(section.id)}>
          <span>{section.number}</span>{section.title}
        </button>
      )}
    </div>)}
  </div>;
}

export function UsageGuidePage({ onOpenModule }: { onOpenModule: (module: ActiveModule) => void }) {
  const mobileContents = useRef<HTMLDetailsElement>(null);
  const scrollRegion = useRef<HTMLDivElement>(null);
  const [preview, setPreview] = useState<{ image: GuideScreenshot; trigger: HTMLElement } | null>(null);
  function selectSection(id: ActiveModule) {
    const heading = document.getElementById(sectionId(id));
    if (mobileContents.current) mobileContents.current.open = false;
    const region = scrollRegion.current;
    if (heading && region) {
      const top = heading.getBoundingClientRect().top - region.getBoundingClientRect().top + region.scrollTop - 20;
      region.scrollTop = Math.max(0, top);
    }
    heading?.focus({ preventScroll: true });
  }
  return <div className="np-usage-guide np-module-page" data-module="usageGuide">
    <ModulePageHeader>{guide.title}</ModulePageHeader>
    <div className="np-guide-scroll" ref={scrollRegion}>
      <div className="np-guide-layout">
        <aside className="np-guide-sidebar" aria-label="功能目录">
          <div className="np-guide-sidebar__title"><List aria-hidden="true" />功能目录</div>
          <GuideContents onSelect={selectSection} />
        </aside>
        <div className="np-guide-content">
          <div className="np-guide-introduction"><BookOpen aria-hidden="true" /><p>{guide.introduction}</p></div>
          <details className="np-guide-mobile-contents" ref={mobileContents}>
            <summary><span><List aria-hidden="true" />功能目录</span><ChevronDown aria-hidden="true" /></summary>
            <GuideContents onSelect={selectSection} />
          </details>
          {guide.sections.map(section => <section className="np-guide-section" key={section.id} aria-labelledby={sectionId(section.id)}>
            <header className="np-guide-section__header">
              <span className="np-guide-section__number">{section.number}</span>
              <div><span className="np-guide-section__group">{section.group}</span><h2 id={sectionId(section.id)} tabIndex={-1}>{section.title}</h2></div>
            </header>
            <div className="np-guide-section__body">
              <p className="np-guide-purpose"><strong>做什么</strong>{section.purpose}</p>
              <h3 className="np-guide-steps-title">怎么用</h3>
              <ol className="np-guide-steps">{section.steps.map((step, index) => <li key={index}>{step}</li>)}</ol>
              {section.screenshots.length > 0 && <GuideScreenshots section={section} onPreview={(image, trigger) => setPreview({ image, trigger })} />}
              <button className="np-guide-open-module" type="button" onClick={() => onOpenModule(section.id)}>
                打开功能<ArrowRight aria-hidden="true" />
              </button>
            </div>
          </section>)}
        </div>
      </div>
    </div>
    {preview && <ScreenshotPreview {...preview} onClose={() => setPreview(null)} />}
  </div>;
}
