import { Hono } from 'hono'
import { renderer } from './renderer'
import { cors } from 'hono/cors'
import * as XLSX from 'xlsx'

const app = new Hono()

// CORS 설정
app.use('/api/*', cors())

app.use(renderer)

// 메인 페이지
app.get('/', (c) => {
  return c.render(
    <div class="container">
      <h1>마케팅 정산 계산기</h1>
      <p class="description">
        엑셀 파일을 업로드하면 고객사별 정산 금액을 자동으로 계산합니다.
      </p>
      <div class="upload-section">
        <input type="file" id="fileInput" accept=".xlsx,.xls" />
        <button id="uploadBtn">파일 업로드 및 계산</button>
      </div>
      <div id="loading" class="loading" style="display:none;">
        <div class="spinner"></div>
        <p>계산 중...</p>
      </div>
      <div id="result" class="result"></div>
      <div id="details" class="details"></div>
      <script src="/static/app.js"></script>
    </div>
  )
})

// 정산 계산 API
app.post('/api/calculate', async (c) => {
  try {
    const formData = await c.req.formData()
    const file = formData.get('file') as File
    
    if (!file) {
      return c.json({ error: '파일이 없습니다.' }, 400)
    }

    // 파일을 ArrayBuffer로 읽기
    const arrayBuffer = await file.arrayBuffer()
    const data = new Uint8Array(arrayBuffer)
    
    // XLSX로 파일 파싱
    const workbook = XLSX.read(data, { type: 'array' })
    const firstSheet = workbook.Sheets[workbook.SheetNames[0]]
    const jsonData = XLSX.utils.sheet_to_json(firstSheet, { header: 1 }) as any[][]

    // 데이터 추출 및 정산 계산
    interface SettlementRow {
      매체사: string
      계정명: string
      합계금액: number
      정산금액: number
    }

    const rows: SettlementRow[] = []
    
    // 2행부터 데이터 추출 (0-based index이므로 2부터 시작)
    for (let i = 2; i < jsonData.length; i++) {
      const row = jsonData[i]
      const 매체사 = row[1]  // B열
      const 계정명 = row[6]  // G열
      const 합계금액 = row[9]  // J열

      // 유효한 데이터만 처리
      if (매체사 && 계정명 && 합계금액 !== undefined && 합계금액 !== null) {
        // '합' 포함된 행과 계정명이 '-'인 행 제외
        if (typeof 매체사 === 'string' && !매체사.includes('합') && 계정명 !== '-') {
          const 금액 = typeof 합계금액 === 'number' ? 합계금액 : parseFloat(String(합계금액))
          
          if (!isNaN(금액)) {
            // 정산율 결정: 네이버 성과형 애드부스트는 5%, 나머지는 10%
            const 정산율 = 매체사.includes('네이버 성과형 애드부스트') ? 0.05 : 0.10
            const 정산금액 = 금액 * 정산율

            rows.push({
              매체사: 매체사,
              계정명: String(계정명),
              합계금액: 금액,
              정산금액: 정산금액
            })
          }
        }
      }
    }

    // 고객사별 집계
    const summaryMap = new Map<string, { 합계금액: number, 정산금액: number, 내역: SettlementRow[] }>()

    rows.forEach(row => {
      if (!summaryMap.has(row.계정명)) {
        summaryMap.set(row.계정명, {
          합계금액: 0,
          정산금액: 0,
          내역: []
        })
      }
      const summary = summaryMap.get(row.계정명)!
      summary.합계금액 += row.합계금액
      summary.정산금액 += row.정산금액
      summary.내역.push(row)
    })

    // 정산금액 내림차순 정렬
    const summaryArray = Array.from(summaryMap.entries())
      .map(([계정명, data]) => ({
        계정명,
        합계금액: data.합계금액,
        정산금액: data.정산금액,
        내역: data.내역
      }))
      .sort((a, b) => b.정산금액 - a.정산금액)

    // 총 합계
    const 전체광고비 = summaryArray.reduce((sum, item) => sum + item.합계금액, 0)
    const 전체정산금액 = summaryArray.reduce((sum, item) => sum + item.정산금액, 0)

    return c.json({
      success: true,
      summary: summaryArray,
      total: {
        전체광고비,
        전체정산금액
      }
    })

  } catch (error) {
    console.error('Error:', error)
    return c.json({ 
      error: '파일 처리 중 오류가 발생했습니다.',
      details: error instanceof Error ? error.message : String(error)
    }, 500)
  }
})

export default app
