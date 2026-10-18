import json
D="2026-10-19"
S={
 "hf_grok":("https://docs.higgsfield.ai/docs/models/grok-image-2/generate-and-edit","Higgsfield"),
 "hf_kling3_pro_i2v":("https://docs.higgsfield.ai/docs/models/kling-3/pro-image-to-video","Higgsfield"),
 "hf_seedance25_i2v":("https://docs.higgsfield.ai/docs/models/seedance-2-5/image-to-video","Higgsfield"),
 "hf_seedance25_r2v":("https://docs.higgsfield.ai/docs/models/seedance-2-5/reference-to-video","Higgsfield"),
 "hf_qwen3_edit":("https://docs.higgsfield.ai/docs/models/qwen-image-3/edit","Higgsfield"),
 "hf_soul2":("https://docs.higgsfield.ai/docs/models/soul-2/generate","Higgsfield"),
 "hf_uploads":("https://docs.higgsfield.ai/docs/concepts/file-uploads","Higgsfield"),
 "hf_billing":("https://docs.higgsfield.ai/docs/concepts/billing-and-retention","Higgsfield"),
 "hf_errors":("https://docs.higgsfield.ai/docs/concepts/errors","Higgsfield"),
 "hf_rate":("https://docs.higgsfield.ai/docs/concepts/rate-limits","Higgsfield"),
 "hf_llms_full":("https://docs.higgsfield.ai/llms-full.txt","Higgsfield (full docs dump; GPT Image 2 has no API endpoint in it)"),
 "hf_mcp_grok":("mcp:user-Higgsfield/models_explore get grok_image_2_0","Higgsfield MCP catalog"),
 "hf_mcp_gpt":("mcp:user-Higgsfield/models_explore get gpt_image_2","Higgsfield MCP catalog"),
 "xai_multi":("https://docs.x.ai/developers/model-capabilities/images/multi-image-editing","xAI"),
 "xai_overview":("https://docs.x.ai/developers/model-capabilities/imagine","xAI"),
 "xai_edit":("https://docs.x.ai/developers/model-capabilities/images/editing","xAI"),
 "xai_prompting":("https://docs.x.ai/developers/model-capabilities/images/prompting-guide","xAI"),
 "xai_news":("https://x.ai/news/grok-imagine-image-2","xAI"),
 "xai_aup":("https://x.ai/legal/acceptable-use-policy","xAI"),
 "oai_edit":("https://developers.openai.com/api/reference/resources/images/methods/edit/","OpenAI"),
 "oai_sdk_edit":("https://github.com/openai/openai-python/blob/main/src/openai/types/image_edit_params.py","OpenAI (official SDK)"),
 "oai_sizes":("https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide","OpenAI"),
 "kling_v3_i2v":("https://kling.ai/document-api/api/video/3-0-omni/image-to-video.md","Kling"),
 "kling_image_api":("https://kling.ai/document-api/api/image/3-0-omni/image-generation.md","Kling"),
 "bp_video_task":("https://ai.byteplus.com/ark/region:ap-southeast-1/docs/create-video-generation-task-api","ByteDance / BytePlus ModelArk"),
 "bp_las":("https://docs.byteplus.com/en/docs/Byteplus_LAS/video_gen_enhanced","ByteDance / BytePlus"),
 "el_models":("https://elevenlabs.io/docs/overview/models","ElevenLabs"),
 "el_pricing":("https://elevenlabs.io/pricing/api","ElevenLabs"),
 "el_sfx_api":("https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert","ElevenLabs"),
 "el_sfx_cap":("https://elevenlabs.io/docs/overview/capabilities/sound-effects","ElevenLabs"),
 "el_music_api":("https://elevenlabs.io/docs/api-reference/music/compose","ElevenLabs"),
 "el_music_cap":("https://elevenlabs.io/docs/overview/capabilities/music","ElevenLabs"),
 "obs_ep05":("/workspace/fail-audit/FINDINGS.md + harness ledgers pr10-95e/216/0d9 (2026-10-18)","Harness live evidence (not a vendor doc)"),
 "obs_estimate":("/workspace/ep05-live/r3b (free /estimate, 2026-10-18)","Harness observation via Higgsfield /estimate"),
 "obs_kling_canary":("hfvg/config.py _load_rate_table comment (live canary 5 s = 0.28 USD)","Harness observation"),
 "harness_seedance_rates":("hfvg/config.py _load_rate_table comment (Higgsfield pricing text)","Harness config, vendor text not re-found today"),
}
sources={k:{"url":u,"publisher":p,"checked":D} for k,(u,p) in S.items()}
U=None
def L(value,basis,src,note=None,hf=U,up=U):
    d={"value":value,"basis":basis,"sources":src}
    if hf is not U: d["higgsfield"]=hf
    if up is not U: d["upstream"]=up
    if note: d["note"]=note
    return d
def UNK(note,src=()):
    return {"value":None,"basis":"unknown","sources":list(src),"note":note}
HF_IMG=["image/jpeg","image/png","image/webp","image/gif"]
M={}
M["xai/grok-imagine-image-2.0"]={
 "kind":"image","provider":"higgsfield","status":"active_default_still",
 "aliases":["grok","grok_image_2_0","grok-imagine-image-2.0"],
 "upstream":{"vendor":"xAI","model_id":"grok-imagine-image-2.0"},
 "request_fields":{"refs":"image_urls","prompt":"prompt","aspect":"aspect_ratio","resolution":"resolution","quality":"quality"},
 "limits":{
  "max_refs":L(5,"stricter_upstream",["xai_multi","xai_overview","xai_news","hf_grok","obs_ep05"],"Higgsfield schema says maxItems 10; xAI says up to five source images. Ep05: every 6-7 ref request ended 'Generation failed' (uncharged), every 5-ref request succeeded.",hf=10,up=5),
  "min_refs":L(0,"higgsfield",["hf_grok"],"0 = text-to-image"),
  "ref_formats":L(HF_IMG,"higgsfield",["hf_uploads","xai_edit"],"Higgsfield upload content types; xAI documents URL/base64/file_id but no format list. Harness normalizes refs to JPEG anyway.",hf=HF_IMG,up=None),
  "ref_max_bytes":UNK("Not documented by Higgsfield or xAI. Higgsfield: 'reference images may be resized during preprocessing'. Harness policy: normalize to JPEG <=1280 px, <=1 MB.",["hf_grok","xai_edit"]),
  "ref_min_px":UNK("Not documented."),
  "ref_max_px":UNK("Not documented; harness normalizes to <=1280 px long edge."),
  "prompt_max_chars":UNK("No limit documented by Higgsfield or xAI. Observed: 2197-char prompts succeeded on the API, 2416 via MCP; the 2470-char _end prompt failed but only together with 6 refs (confounded).",["hf_grok","xai_prompting","obs_ep05"]),
  "aspect_ratios":L(["auto","1:1","1:2","2:1","3:2","2:3","4:3","3:4","16:9","9:16"],"higgsfield",["hf_grok","hf_mcp_grok"],"xAI: default follows the first input image; harness always sends an explicit ratio."),
  "resolutions":L(["1k","2k"],"higgsfield",["hf_grok","xai_overview"]),
  "quality":L(["low","medium"],"stricter_higgsfield",["hf_grok","xai_prompting"],"xAI also accepts 'auto'; Higgsfield only low|medium.",hf=["low","medium"],up=["low","medium","auto"]),
  "outputs_per_request":L(1,"higgsfield",["hf_grok"]),
 },
 "cost":{"unit":"image","observed":{"credits":0.96,"usd":0.06,"params":"1k medium 9:16, 5 refs","sources":["obs_estimate"]},
         "upstream_list":{"usd":0.04,"note":"xAI per output image; edits are also billed for input images","sources":["xai_overview"]},
         "authoritative":"POST /estimate/xai/grok-imagine-image-2.0 with the exact submit body (free)","sources":["hf_billing"]},
 "moderation":[
  {"quirk":"Over-limit or rejected inputs end as status 'failed' with only error 'Generation failed' (no reason), ~6 s, uncharged. Treat >5 refs as a preflight refusal, never a retry.","sources":["hf_errors","obs_ep05"]},
  {"quirk":"Requests may end 'nsfw' (uncharged).","sources":["hf_billing"]},
  {"quirk":"xAI AUP forbids sexualized/undressing edits of real people, deceptive impersonation and pornographic likenesses.","sources":["xai_aup"]}],
}
M["gpt_image_2"]={
 "kind":"image","provider":"higgsfield_mcp","status":"mcp_only_not_on_api",
 "aliases":["gpt-image-2","gpt_image_2"],
 "upstream":{"vendor":"OpenAI","model_id":"gpt-image-2"},
 "request_fields":{"refs":"medias[role=image] (MCP)","prompt":"prompt","aspect":"aspect_ratio","resolution":"resolution","quality":"quality"},
 "limits":{
  "max_refs":L(16,"upstream",["oai_edit","oai_sdk_edit","hf_mcp_gpt"],"OpenAI: up to 16 input images. The Higgsfield MCP catalog lists no count. Manual Ep02-04 runs used up to 16 with no failures.",hf=None,up=16),
  "min_refs":L(0,"higgsfield",["hf_mcp_gpt"]),
  "ref_formats":L(["image/png","image/webp","image/jpeg"],"stricter_upstream",["oai_edit","hf_uploads"],"OpenAI: png, webp, jpg (no gif).",hf=HF_IMG,up=["png","webp","jpg"]),
  "ref_max_bytes":L(50*1024*1024-1,"upstream",["oai_edit"],"OpenAI: each image less than 50 MB."),
  "ref_min_px":UNK("Not documented for inputs."),
  "ref_max_px":UNK("Not documented for inputs."),
  "prompt_max_chars":L(32000,"upstream",["oai_edit","oai_sdk_edit"]),
  "aspect_ratios":L(["1:1","4:3","3:4","16:9","21:9","9:16","3:2","2:3"],"higgsfield",["hf_mcp_gpt","oai_sizes"],"OpenAI ratio must be between 1:3 and 3:1; all MCP values satisfy it."),
  "resolutions":L(["1k","2k","4k"],"higgsfield",["hf_mcp_gpt","oai_sizes"],"OpenAI: above 2560x1440 is experimental; max 3840x2160."),
  "quality":L(["low","medium","high"],"higgsfield",["hf_mcp_gpt"]),
  "transparent_background":L(False,"upstream",["oai_edit"],"gpt-image-2 rejects background=transparent."),
 },
 "cost":{"unit":"image","observed":None,"note":"Not available on the Higgsfield API path. hfvg MODEL_PATH_GPT_IMAGE_2 defaults to '' and the provider refuses; the in-repo credit numbers are estimates.","sources":["hf_llms_full"]},
 "moderation":[{"quirk":"OpenAI content policy applies; refusals surface as provider errors. Not characterized for this harness.","sources":[]}],
 "harness_note":"Offered via the CLI default (--model gpt_image_2) and legacy tables, but there is no Higgsfield API endpoint. Preflight must refuse it on the API path with a creator-facing message until an endpoint exists.",
}
M["higgsfield-ai/soul/v2/standard"]={
 "kind":"image","provider":"higgsfield","status":"active_ref_generation","aliases":["soul_v2","soul-2","soul"],
 "upstream":{"vendor":"Higgsfield (first-party)","model_id":"soul/v2/standard"},
 "limits":{
  "max_refs":L(0,"higgsfield",["hf_soul2"],"No image refs; identity comes only from custom_reference_id, a trained Soul ID owned by the account."),
  "prompt_max_chars":UNK("Not documented.",["hf_soul2"]),
  "aspect_ratios":L(["9:16","16:9","4:3","3:4","1:1","2:3","3:2"],"higgsfield",["hf_soul2"]),
  "resolutions":L(["720p","1080p"],"higgsfield",["hf_soul2"]),
  "batch_size":L([1,4],"higgsfield",["hf_soul2"]),
  "custom_reference_strength":L({"min_exclusive":0,"max":1},"higgsfield",["hf_soul2"],"The schema accepts 0, but the runtime fails with 0 when a character reference is used."),
 },
 "cost":{"unit":"image","illustrative":{"credits":1.5,"usd":0.094,"sources":["hf_billing"]},"harness_table":0.09,"note":"Use /estimate; the docs example is illustrative only."},
 "moderation":[{"quirk":"Short general-style prompts may be auto-enhanced even with enhance_prompt=false; style_strength currently has no effect.","sources":["hf_soul2"]}],
}
M["alibaba/qwen-image-3/edit"]={
 "kind":"image","provider":"higgsfield","status":"configured_cap_only_not_routed","aliases":["qwen-image-3"],
 "upstream":{"vendor":"Alibaba","model_id":"qwen-image-3 edit"},
 "limits":{
  "max_refs":L(3,"higgsfield",["hf_qwen3_edit"],"Upstream Alibaba doc not checked (UNKNOWN); Higgsfield value used."),
  "min_refs":L(1,"higgsfield",["hf_qwen3_edit"]),
  "prompt_max_chars":UNK("Alibaba recommends at most 4,500 tokens (via Higgsfield); no character limit.",["hf_qwen3_edit"]),
  "aspect_ratios":L(["1:1","2:3","3:2","3:4","4:3","7:9","9:7","9:16","16:9","21:9"],"higgsfield",["hf_qwen3_edit"]),
  "resolutions":L(["1k","2k"],"higgsfield",["hf_qwen3_edit"]),
 },
 "cost":{"unit":"image","observed":None,"note":"UNKNOWN; use /estimate"},
 "moderation":[],
}
FRAME_FMT_K=["image/jpeg","image/png"]
M["kling-video/v3.0/pro/image-to-video"]={
 "kind":"video","provider":"higgsfield","status":"active_default_clip","aliases":["kling","kling_3.0","kling-3.0-pro","kling_video"],
 "upstream":{"vendor":"Kling","model_id":"kling-v3 (3.0) image-to-video"},
 "request_fields":{"start_frame":"image_url","end_frame":"last_image_url","prompt":"prompt","duration":"duration","audio":"sound"},
 "limits":{
  "max_refs":L(0,"higgsfield",["hf_kling3_pro_i2v"],"Only first and last frames. 'elements' takes Kling element ids owned by the account, not image refs. The harness cap table says 3 but nothing sends refs."),
  "start_frame":L("required","higgsfield",["hf_kling3_pro_i2v","kling_v3_i2v"]),
  "end_frame":L("optional","higgsfield",["hf_kling3_pro_i2v","kling_v3_i2v"],"Last-frame-only is not supported upstream."),
  "frame_formats":L(FRAME_FMT_K,"stricter_upstream",["kling_v3_i2v","hf_uploads"],"Kling accepts jpg/jpeg/png only; Higgsfield uploads also accept webp/gif.",hf=HF_IMG,up=["jpg","jpeg","png"]),
  "frame_max_bytes":L(10*1024*1024,"conservative_upstream",["kling_v3_i2v","kling_image_api"],"Official Kling docs disagree: 3.0 Omni i2v says 50 MB, the Kling image API says 10 MB. 10 MB is used until confirmed.",up=[50*1024*1024,10*1024*1024]),
  "frame_min_px":L(300,"upstream",["kling_v3_i2v"]),
  "frame_aspect_range":L([0.4,2.5],"upstream",["kling_v3_i2v"],"1:2.5 to 2.5:1"),
  "prompt_max_chars":L(2500,"stricter_higgsfield",["hf_kling3_pro_i2v","kling_v3_i2v"],"Higgsfield: keep within 2,500; longer is TRUNCATED silently. Kling: max 3072, recommended 2500. Refuse rather than truncate.",hf=2500,up=3072),
  "prompt_required":L(True,"higgsfield",["hf_kling3_pro_i2v"],"A non-empty top-level prompt is always required."),
  "duration_s":L({"min":3,"max":15,"integer":True},"higgsfield",["hf_kling3_pro_i2v","kling_v3_i2v"],"hfvg docstring says 3-12; docs say 3-15."),
  "multi_prompt":L({"max_items":6,"item_prompt_max_chars":512,"item_duration_s":[1,15]},"higgsfield",["hf_kling3_pro_i2v","kling_v3_i2v"]),
  "aspect_ratios":L(["follows_start_frame"],"higgsfield",["hf_kling3_pro_i2v"],"No aspect parameter: the output follows image_url."),
  "resolutions":UNK("Not a parameter on the Higgsfield pro endpoint (the harness assumes native 1080p). Kling 3.0 upstream offers 720p/1080p/4k.",["hf_kling3_pro_i2v","kling_v3_i2v"]),
  "audio":L({"param":"sound","values":["on","off"],"default":"on","harness_sends":"off"},"higgsfield",["hf_kling3_pro_i2v"],"Defaults to ON at Higgsfield; the harness must keep sending off."),
  "cfg_scale":L([0,1],"higgsfield",["hf_kling3_pro_i2v"]),
 },
 "cost":{"unit":"second","observed":{"usd_per_s":0.056,"sources":["obs_kling_canary"]},"authoritative":"POST /estimate/kling-video/v3.0/pro/image-to-video (returns usd)","sources":["hf_billing"]},
 "moderation":[{"quirk":"Kling tasks can fail on platform content risk control; via Higgsfield this surfaces as failed/nsfw (uncharged).","sources":["kling_v3_i2v","hf_billing"]},
               {"quirk":"Element ids not owned by the calling account are rejected.","sources":["hf_kling3_pro_i2v"]}],
}
SD_FMT_UP=["jpeg","png","webp","bmp","tiff","gif","heic","heif"]
M["bytedance/seedance-2.5/image-to-video"]={
 "kind":"video","provider":"higgsfield","status":"active_routed","aliases":["seedance","seedance_2.5","seedance-2.5","seedance-2-5","seedance_video"],
 "upstream":{"vendor":"ByteDance (BytePlus ModelArk)","model_id":"dreamina-seedance-2.5"},
 "request_fields":{"start_frame":"image_url","end_frame":"end_image_url","prompt":"prompt","duration":"duration","resolution":"resolution","audio":"generate_audio"},
 "limits":{
  "max_refs":L(0,"higgsfield",["hf_seedance25_i2v"],"First and last frame only (reference mode is a separate endpoint)."),
  "start_frame":L("required","higgsfield",["hf_seedance25_i2v"]),
  "end_frame":L("optional","higgsfield",["hf_seedance25_i2v","bp_video_task"],"If the start and end aspect ratios differ, the last frame is cropped to fit."),
  "frame_formats":L(HF_IMG,"intersection",["hf_uploads","bp_video_task"],"Upstream also takes bmp/tiff/heic/heif, but Higgsfield uploads take only jpeg/png/webp/gif.",hf=HF_IMG,up=SD_FMT_UP),
  "frame_max_bytes":L(30*1024*1024-1,"upstream",["bp_video_task","bp_las"],"<30 MB per image; request body <=64 MB."),
  "frame_min_px":L(300,"upstream",["bp_video_task"]),
  "frame_max_px":L(6000,"upstream",["bp_video_task"]),
  "frame_aspect_range":L([0.4,2.5],"upstream",["bp_video_task"]),
  "prompt_max_chars":UNK("No hard limit documented. BytePlus RECOMMENDS at most 500 Chinese characters or 1,000 English words. Higgsfield: prompt optional, minLength 1 if present.",["bp_video_task","hf_seedance25_i2v"]),
  "duration_s":L({"min":4,"max":30,"integer":True},"higgsfield",["hf_seedance25_i2v","bp_video_task"]),
  "resolutions":L(["480p","720p","1080p"],"higgsfield",["hf_seedance25_i2v"]),
  "aspect_ratios":L(["follows_start_frame"],"higgsfield",["hf_seedance25_i2v"],"Output framing follows image_url."),
  "audio":L({"param":"generate_audio","default":True,"harness_sends":False},"higgsfield",["hf_seedance25_i2v"]),
  "media_urls":L("public https only; asset:// rejected","higgsfield",["hf_seedance25_i2v"]),
 },
 "harness_policy":{"min_duration_s":4,"blocked_tags":["wet"],"multi_animal":"count>1 must use Kling","tier_resolution":{"draft":"480p","final":"1080p"}},
 "cost":{"unit":"second","list_usd_per_s":{"480p":0.2056,"720p":0.4622,"1080p":1.1372},"sources":["harness_seedance_rates"],"note":"The /estimate endpoint returns a pricing description, not USD, for Seedance; list price before discounts. Re-verify."},
 "moderation":[{"quirk":"Seedance 2.x does not accept reference images or videos containing REAL human faces (portrait assets need the verified asset library). Cartoon/animal content is unaffected.","sources":["bp_video_task","bp_las"]},
               {"quirk":"failed/nsfw are uncharged.","sources":["hf_billing"]}],
}
M["bytedance/seedance-2.5/reference-to-video"]={
 "kind":"video","provider":"higgsfield","status":"configured_cap_only_not_routed","aliases":[],
 "upstream":{"vendor":"ByteDance (BytePlus ModelArk)","model_id":"dreamina-seedance-2.5 omni reference"},
 "limits":{
  "max_refs":L(8,"harness_policy",["hf_seedance25_r2v","bp_video_task"],"Higgsfield and upstream: 1-30 images (50 media total). The harness caps at 8 by policy.",hf=30,up=30),
  "max_videos":L(10,"higgsfield",["hf_seedance25_r2v"]),
  "max_audios":L(10,"higgsfield",["hf_seedance25_r2v","bp_las"],"Upstream: each audio 2-30 s, total <=30 s, <=15 MB, wav/mp3."),
  "max_media_total":L(50,"higgsfield",["hf_seedance25_r2v"]),
  "duration_s":L({"min":4,"max":30,"integer":True},"higgsfield",["hf_seedance25_r2v"]),
  "resolutions":L(["480p","720p","1080p"],"higgsfield",["hf_seedance25_r2v"]),
  "aspect_ratios":L(["16:9","4:3","1:1","3:4","9:16","21:9"],"higgsfield",["hf_seedance25_r2v"]),
  "frame_formats":L(HF_IMG,"intersection",["hf_uploads","bp_video_task"]),
  "frame_max_bytes":L(30*1024*1024-1,"upstream",["bp_video_task"]),
  "frame_min_px":L(300,"upstream",["bp_video_task"]),"frame_max_px":L(6000,"upstream",["bp_video_task"]),
  "frame_aspect_range":L([0.4,2.5],"upstream",["bp_video_task"]),
  "prompt_max_chars":UNK("Recommended <=1,000 English words (BytePlus); no hard limit.",["bp_video_task"]),
 },
 "cost":{"unit":"second","note":"UNKNOWN for reference mode; use /estimate"},
 "moderation":[{"quirk":"No real human faces in references (Seedance 2.x).","sources":["bp_video_task"]}],
}
M["eleven_multilingual_v2"]={
 "kind":"tts","provider":"elevenlabs","status":"active_default_tts","aliases":[],
 "upstream":{"vendor":"ElevenLabs","model_id":"eleven_multilingual_v2"},
 "limits":{"text_max_chars":L(10000,"upstream",["el_models","el_pricing"],"About 10 minutes of audio; split longer text.")},
 "cost":{"unit":"1k_chars","usd":0.08,"sources":["el_pricing"],"harness_table_note":"hfvg AUDIO_COSTS uses an estimated 30 cr/min; replace with per-character pricing."},
 "moderation":[],
}
M["eleven_turbo_v2_5"]={
 "kind":"tts","provider":"elevenlabs","status":"deprecated_upstream","aliases":[],
 "upstream":{"vendor":"ElevenLabs","model_id":"eleven_turbo_v2_5","replacement":"eleven_flash_v2_5"},
 "limits":{"text_max_chars":L(40000,"upstream",["el_pricing","el_models"],"The pricing page lists 'Flash / Turbo: 40,000 character limit'. The models page lists only flash_v2_5 (40,000) and calls turbo functionally equivalent.")},
 "cost":{"unit":"1k_chars","usd":0.04,"sources":["el_pricing"]},
 "moderation":[],"harness_note":"Deprecated: migrate to eleven_flash_v2_5.",
}
M["sound_generation"]={
 "kind":"sfx","provider":"elevenlabs","status":"active","aliases":["eleven_text_to_sound_v2"],
 "upstream":{"vendor":"ElevenLabs","model_id":"eleven_text_to_sound_v2","endpoint":"POST /v1/sound-generation"},
 "limits":{
  "duration_s":L({"min":0.5,"max":30,"integer":False},"stricter_upstream_pages",["el_sfx_api","el_sfx_cap"],"The API reference says 0.5-30; the capabilities page says 0.1-30. 0.5 is used.",up=[[0.5,30],[0.1,30]]),
  "text_max_chars":UNK("Not documented.",["el_sfx_api"]),
  "prompt_influence":L([0,1],"upstream",["el_sfx_api"]),
  "loop":L("eleven_text_to_sound_v2 only","upstream",["el_sfx_api"]),
 },
 "cost":{"unit":"minute","usd":0.12,"credits_per_s_when_duration_set":40,"sources":["el_pricing","el_sfx_cap"],"note":"Billed per generation (pricing FAQ)."},
 "moderation":[],
}
M["eleven_music"]={
 "kind":"music","provider":"elevenlabs","status":"alias_invalid_upstream_id","aliases":["music_v1","music_v2","music_v2_5"],
 "upstream":{"vendor":"ElevenLabs","model_id":"music_v1 (API default); music_v2, music_v2_5 also valid","endpoint":"POST /v1/music"},
 "limits":{
  "duration_ms":L({"min":3000,"max":300000},"stricter_upstream_pages",["el_music_api","el_music_cap","el_pricing"],"The API reference allows 3,000-600,000 ms; the capabilities FAQ and pricing page say 5 minutes max. 300,000 is used.",up=[[3000,600000],[3000,300000]]),
  "prompt_max_chars":UNK("Not documented.",["el_music_api"]),
  "lyrics":L({"lines_per_section_max":30,"chars_per_line_max":200,"section_name_chars":[1,100]},"upstream",["el_music_api"]),
  "exclusive_inputs":L("prompt XOR composition_plan; force_instrumental and music_length_ms only with prompt","upstream",["el_music_api"]),
 },
 "cost":{"unit":"minute","usd":0.15,"sources":["el_pricing"],"note":"Billed per generation."},
 "moderation":[{"quirk":"Uploaded reference audio (about 30 s) is screened for copyright before use.","sources":["el_music_cap"]}],
 "harness_note":"'eleven_music' is NOT a valid ElevenLabs model_id. Map it to music_v1 (or pin music_v2_5) before any live call.",
}
out={"schema_version":1,"checked":D,"recheck_interval_days":30,
 "precedence":"When Higgsfield and the upstream vendor differ, value = the stricter one (basis says which). 'unknown' = no official value: preflight never refuses on it, only shows it.",
 "shared":{"higgsfield":{"upload_content_types":HF_IMG+["audio/wav","audio/x-wav","video/mp4"],"upload_url_ttl_s":3600,"output_retention_days_min":7,
   "failed_or_nsfw_charged":False,"concurrency_error":"400 'Maximum number of concurrent requests (N) has been reached'","sources":["hf_uploads","hf_billing","hf_errors","hf_rate"]}},
 "models":M,"sources":sources}
json.dump(out,open("/workspace/model-caps/model_caps.json","w"),indent=1)
print(len(M),"models",len(sources),"sources")
