'use strict';
// Local stdin/stdout worker. Never emits recognized text or fetches image URLs.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const phrases = ['test123','TEST123','Test 123','test123!@#',
  'ASCII symbols: !"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~'];

function validateJob(job) {
  if (!job || Object.keys(job).sort().join(',') !== 'expectedText,id,imagePath' ||
      !Number.isSafeInteger(job.id) || job.id < 1 || typeof job.expectedText !== 'string' ||
      !phrases.some(value => value.startsWith(job.expectedText)) ||
      typeof job.imagePath !== 'string' || !path.isAbsolute(job.imagePath) ||
      path.extname(job.imagePath).toLowerCase() !== '.png') throw Error('invalid local OCR job');
}

function matchText(raw, expected) {
  const text = typeof raw === 'string' ? raw.replace(/\r?\n$/, '') : null;
  const match = text === expected;
  return {text_matches:match, recognized_length:match ? expected.length : null};
}

async function main() {
  let worker;
  const reply = value => process.stdout.write(JSON.stringify(value)+'\n');
  try {
    const langPath = process.argv[2];
    if (!langPath || !path.isAbsolute(langPath)) throw Error('local language path required');
    const data = fs.readFileSync(path.join(langPath,'eng.traineddata'));
    if (crypto.createHash('sha256').update(data).digest('hex') !==
        '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2') throw Error('language hash mismatch');
    const {createWorker,OEM,PSM} = require('tesseract.js');
    worker = await createWorker('eng',OEM.LSTM_ONLY,{langPath,gzip:false,cacheMethod:'none',
      logger:()=>{},errorHandler:()=>{}});
    await worker.setParameters({tessedit_pageseg_mode:PSM.SINGLE_LINE,preserve_interword_spaces:'1'});
    reply({ready:true});
    const lines = require('node:readline').createInterface({input:process.stdin,crlfDelay:Infinity});
    for await (const line of lines) {
      let job;
      try {
        if (line.length > 4096) throw Error('job too long');
        job=JSON.parse(line); validateJob(job);
        const bytes=fs.readFileSync(job.imagePath);
        if (bytes.length>4*1024*1024 || !bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))) throw Error('PNG required');
        const {data:result}=await worker.recognize(bytes);
        reply({id:job.id,...matchText(result.text,job.expectedText)});
      } catch (_) {
        reply({id:job && Number.isSafeInteger(job.id) ? job.id : null,error_code:'LOCAL_OCR_FAILED'});
      }
    }
  } catch (_) {reply({ready:false,error_code:'LOCAL_OCR_UNAVAILABLE'});process.exitCode=2;}
  finally {if(worker) await worker.terminate();}
}

module.exports={validateJob,matchText};
if(require.main===module) main();
