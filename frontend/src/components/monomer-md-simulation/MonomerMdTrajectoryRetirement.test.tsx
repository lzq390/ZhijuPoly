// @vitest-environment jsdom
import {act,cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {MonomerMdResultsPanel} from './MonomerMdResultsPanel';
import * as codec from './trajectoryTimeline';
import type {MonomerMdJobResponse,MonomerMdSimulationResult,MonomerMdTrajectoryTimeline} from '../../types';

afterEach(()=>{cleanup();vi.restoreAllMocks();});

it.each(['resolve','reject'] as const)('retires pending native timeline decoding before a new identity view (%s)',async outcome=>{
 const original=codec.decodeMonomerMdTrajectoryTimeline;
 let finish!:(value:Awaited<ReturnType<typeof original>>)=>void,fail!:(error:Error)=>void;
 vi.spyOn(codec,'decodeMonomerMdTrajectoryTimeline').mockImplementationOnce(()=>new Promise((resolve,reject)=>{finish=resolve;fail=reject;}));
 const timeline={schema_version:1,source_frame_count:10,sampled_frame_count:2,total_atoms:4,sampled_points:2,
  coordinate_unit:'angstrom',coordinate_scale:0.01,coordinate_encoding:'int16-delta-gzip-base64',coordinate_byte_order:'little',
  decoded_byte_length:24,compressed_byte_length:44,sampling_strategy:'whole_residue_component_stratified',
  atoms:[{atom_id:1,chain_id:1,atom_type:'C',element:'C'},{atom_id:2,chain_id:1,atom_type:'O',element:'O'}],
  frames:[{frame_index:0,time_ps:1},{frame_index:9,time_ps:10}],coordinates:'H4sIAAAAAAACA0thOMGgwziB8QtjBBMXgwiDHIMGgxGDDQMA3AVZwhgAAAA='} as MonomerMdTrajectoryTimeline;
 const job={job_id:'a'.repeat(32),status:'completed',run_mode:'formal',protocol:'Density',progress_percent:100,
  requested_steps:1500000,completed_steps:1500000,created_at:'2026-09-24T00:00:00Z'} as MonomerMdJobResponse;
 const result={summary:{},artifacts:[],visualization:{schema_version:3,status:'complete',default_stage_id:'npt',
  stages:[{stage_id:'npt',label:'NPT',trajectory_timeline:timeline,warnings:[]}],warnings:[]}} as MonomerMdSimulationResult;
 const props={job,result,isLoading:false,error:null,cancelling:false,deleting:false,onCancel:vi.fn(),onDelete:vi.fn(),onClear:vi.fn()};
 const view=render(<MonomerMdResultsPanel {...props}/>);
 fireEvent.click(screen.getByRole('tab',{name:/构象/}));
 expect(screen.getByText('正在加载轨迹关键帧')).toBeTruthy();
 // Identity retirement removes the old private result before decompression ends.
 view.rerender(<MonomerMdResultsPanel {...props} job={null} result={null}/>);
 await act(async()=>{if(outcome==='reject')fail(new Error('retired-owner-decode-error'));else finish(await original(timeline));});
 expect(screen.queryByRole('slider',{name:'选择构象关键帧'})).toBeNull();
 expect(screen.queryByText('retired-owner-decode-error')).toBeNull();
 expect(view.container.textContent).not.toContain('a'.repeat(32));
 expect(view.container.querySelector('.np-mmd-conformation-fallback')).toBeNull();
});
